"""Where the replay clock lives between requests.

Pulled out of the API module so the agent's tools can reach the same running
replay without importing the web layer. Without this split, `api` imports
`agent` to answer chat and `agent` imports `api` to read the board, which is a
cycle Python will refuse at the least convenient moment.

The registry is process-local. Alerts and the cursor are also written to
Postgres, because a serverless host starts a fresh process on the next
request: without that write, Jump lands on one instance and GET /alerts
opens at 07:30 with an empty list on another.
"""

from __future__ import annotations

import asyncio
import json
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any

from app.config import LIVE_ANCHOR, LIVE_RATE, MODE
from app.core.alerts import Alert
from app.replay import Replay
from app import store

LIVE_SPEED = LIVE_RATE / 60.0
"""`speed` is replay minutes per real second, so real time is a sixtieth."""


@dataclass
class Session:
    """One replay, its clock, and the speed it is running at."""

    replay: Replay
    speed: float = 60.0
    """Replayed minutes per real second. 60 puts a 2.5-hour morning in about
    two and a half minutes, which is roughly a demo's attention span."""

    running: bool = False
    started_wall: datetime | None = None
    started_clock: datetime | None = None
    alert_ids: dict[str, int] = field(default_factory=dict)
    task: asyncio.Task | None = None
    timeline: Any = None
    """A `Timeline` once the morning has been captured for scrubbing."""

    mode: str = MODE
    """`replay` or `live`. Only affects how fast the clock runs and whether it
    starts by itself; every read downstream is guarded by `now` regardless."""

    @property
    def live(self) -> bool:
        return self.mode == "live"

    def target_clock(self, wall: datetime | None = None) -> datetime:
        """Where the clock should be, derived from elapsed wall time.

        Deriving the position rather than accumulating sleeps is what keeps a
        live clock honest. A tick that takes longer than its budget, or an
        event loop busy narrating, would otherwise leave the board permanently
        behind by however much it once fell behind. Computed from the start
        instant, it catches up instead.
        """
        if self.started_wall is None or self.started_clock is None:
            return self.replay.now
        elapsed = ((wall or datetime.now()) - self.started_wall).total_seconds()
        return self.started_clock + timedelta(minutes=elapsed * self.speed)

    @property
    def key(self) -> str:
        return session_key(
            self.replay.business_unit,
            self.replay.office,
            self.replay.shift_date,
            self.replay.shift_type,
        )

    def persist(self) -> None:
        """Write the cursor and every alert, remembering each row id.

        Hydrate first so a seek that rebuilt alerts from first principles
        picks up the ids and prose already in Postgres, instead of saving
        a blank narrative over a row the last instance already wrote.
        """
        self.hydrate()
        for queue, alert in self.replay.alerts.items():
            self.alert_ids[queue] = store.save_alert(alert)
        if self.replay.now is not None:
            store.save_clock(
                self.replay.office,
                self.replay.shift_date,
                self.replay.shift_type,
                self.replay.now,
                running=self.running,
            )

    def hydrate(self) -> None:
        """Attach row ids and cached prose after a seek rebuilt the alerts."""
        rows = store.load_alerts(
            self.replay.office, self.replay.shift_date, self.replay.shift_type
        )
        by_queue = {row["queue"]: row for row in rows}
        for queue, alert in self.replay.alerts.items():
            row = by_queue.get(queue)
            if row:
                self.alert_ids[queue] = row["id"]
            cached = store.find_cached_narrative(alert.payload_hash())
            drafts = (cached or {}).get("drafts") if cached else None
            if drafts is None and row:
                drafts = row.get("drafts")
            if isinstance(drafts, str):
                drafts = json.loads(drafts) if drafts else {}
            drafts = drafts or {}
            narrative = (cached or {}).get("narrative") if cached else None
            if not narrative and row and row.get("payload_hash") == alert.payload_hash():
                narrative = row.get("narrative")
            if narrative and alert.narrative is None:
                alert.narrative = narrative
                if drafts:
                    alert.drafts = drafts
                alert.mark_narrated(self.replay.now)

    def alert_for(self, alert_id: int) -> tuple[str, Alert] | None:
        # Ollama/LiteLLM pass tool arguments as strings; 1851 != "1851"
        # would miss every alert and the narrator would save nothing.
        try:
            alert_id = int(alert_id)
        except (TypeError, ValueError):
            return None
        for queue, known_id in self.alert_ids.items():
            if known_id == alert_id:
                return queue, self.replay.alerts[queue]
        return None

    def id_for(self, queue: str) -> int | None:
        return self.alert_ids.get(queue)


def session_key(bu: str, office: str, shift_date: date, shift_type: str) -> str:
    return f"{bu}|{office}|{shift_date.isoformat()}|{shift_type}"


def live_start(replay: Replay) -> datetime:
    """Where a live clock begins, clamped to the window the board watches.

    An anchor of `now` maps the wall clock's own time-of-day onto the shift
    date, which is what a consumer of real trip events would see and what a
    real deployment wants. An HH:MM anchor pins the start instead, which is
    what a demo wants: the presenter's afternoon is not interesting, the
    quarter-hour before the shift starts is.
    """
    anchor = LIVE_ANCHOR.strip().lower()
    if anchor in {"", "now", "wall"}:
        at = datetime.combine(replay.shift_date, datetime.now().time())
    else:
        at = datetime.combine(replay.shift_date, time.fromisoformat(anchor))
    return min(max(at, replay.clock_start), replay.clock_end)


SESSIONS: dict[str, Session] = {}


class SessionMissing(LookupError):
    """No replay is running for the requested shift."""


class RosterMissing(LookupError):
    """Nobody is rostered for the requested shift."""


def get_session(
    business_unit: str,
    office: str,
    shift_date: date,
    shift_type: str,
    create: bool = True,
    mode: str | None = None,
    restore: bool = True,
) -> Session:
    """Fetch the session for one tenant/office/shift, creating it if asked.

    `restore` seeks a new process to the last persisted clock. Rewind
    (`_recreate`) turns it off so the caller can seek from 07:30 itself.
    """
    key = session_key(business_unit, office, shift_date, shift_type)
    session = SESSIONS.get(key)
    if session is None:
        if not create:
            raise SessionMissing(f"no replay running for {key}")
        replay = Replay(
            shift_date=shift_date,
            office=office,
            business_unit=business_unit,
            shift_type=shift_type,
        )
        if not replay.legs:
            raise RosterMissing(
                f"no roster rows for {office} {shift_type} on {shift_date}"
            )
        session = Session(replay=replay, mode=mode or MODE)
        SESSIONS[key] = session
        if restore:
            stored = store.load_clock(office, shift_date, shift_type)
            if stored is None and session.live:
                # A live board opens where the wall clock says it is, with the
                # morning behind it already stepped through, so the alert history
                # and the feed are the ones that actually built up.
                session.speed = LIVE_SPEED
                replay.seek(live_start(replay))
            else:
                session = align_to_persisted(session)
        return session
    if restore:
        return align_to_persisted(session)
    return session


def align_to_persisted(session: Session) -> Session:
    """Match this process's cursor to the last persist.

    Jump writes the clock and `running=false`. A warm instance that was
    still ticking live would otherwise catch up past the landing and
    overwrite it.
    """
    stored = store.load_clock(
        session.replay.office, session.replay.shift_date, session.replay.shift_type
    )
    if stored is None:
        return session
    clock = stored["clock"]
    if clock < session.replay.now:
        drop_session(
            session.replay.business_unit,
            session.replay.office,
            session.replay.shift_date,
            session.replay.shift_type,
        )
        return get_session(
            session.replay.business_unit,
            session.replay.office,
            session.replay.shift_date,
            session.replay.shift_type,
        )
    if clock > session.replay.now:
        session.replay.seek(clock)
    if stored["running"]:
        session.running = True
        if session.started_wall is None:
            session.started_wall = datetime.now()
            session.started_clock = session.replay.now
    else:
        session.running = False
        session.started_wall = None
        session.started_clock = None
        if session.task and not session.task.done():
            session.task.cancel()
        session.task = None
    return session


def drop_session(bu: str, office: str, shift_date: date, shift_type: str) -> Session | None:
    """Remove a session, cancelling its clock if it is running."""
    session = SESSIONS.pop(session_key(bu, office, shift_date, shift_type), None)
    if session and session.task:
        session.task.cancel()
    return session


# --------------------------------------------------------------- tool context
#
# The agent's tools are plain functions with no argument for "which shift are
# we talking about". Threading four scope parameters through every tool
# signature would put them in the model's schema, where they are noise at best
# and something for it to hallucinate at worst. A context variable set by the
# caller keeps the scope out of the model's hands entirely: the agent can only
# ever act on the shift it was invoked for.

_CURRENT: ContextVar[Session | None] = ContextVar("current_session", default=None)


def bind_session(session: Session):
    """Make `session` the one the agent's tools operate on."""
    return _CURRENT.set(session)


def unbind_session(token) -> None:
    _CURRENT.reset(token)


def current_session() -> Session:
    """The session the tools should read. Raises rather than guessing."""
    session = _CURRENT.get()
    if session is None:
        raise SessionMissing(
            "no shift is bound; tools must be called inside an agent invocation"
        )
    return session
