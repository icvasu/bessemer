"""HTTP surface for the shift board.

Thin by design. Every endpoint either reads the reasoning core or records a
decision; none of them contain judgement of their own. If a number appears here
that is not computed upstream, that is a bug.

Two design notes worth stating.

**The replay is a session, not a request.** A board that recomputed the whole
morning on each poll would be both slow and wrong: alert lifecycle depends on
what was true a tick ago, so the clock has to live somewhere. It lives in a
process-local session keyed by tenant, office and shift.

**Every endpoint is scoped by business unit and office.** Not because this
prototype serves more than one, but because a multi-tenancy claim that is not
enforced at the API is not a claim, it is a hope. Defaults come from config so
the demo needs no arguments.

Run:  uv run uvicorn app.api:app --reload
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from pathlib import Path

from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse

import logging

from app.config import (
    AUTOSTART,
    BUSINESS_UNIT,
    CLOCK_END,
    CLOCK_START,
    DEMO_DATE,
    JUMP_TO,
    LIVE_RATE,
    MODE,
    OFFICE,
    SHIFT_TYPE,
)
from app.core.alerts import Alert, Pathway, Status
from app.core.remediation import record_cover
from app.sessions import (
    LIVE_SPEED,
    SESSIONS,
    RosterMissing,
    Session,
    SessionMissing,
    drop_session,
    get_session,
    live_start,
    session_key,
)
from app import store

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # A live board should be moving before anyone opens it, which means the
    # clock cannot wait for the first HTTP request to start it. A missing
    # roster must not take the process down with it: the API still needs to
    # come up and say what is wrong.
    if AUTOSTART or MODE == "live":
        try:
            await _autostart()
        except Exception as exc:  # noqa: BLE001 - the API must boot regardless
            log.warning("could not start the configured shift: %s", exc)
    yield
    for session in SESSIONS.values():
        if session.task:
            session.task.cancel()


async def _autostart() -> None:
    """Put the configured shift on the clock as the process comes up."""
    session = get_session(
        BUSINESS_UNIT, OFFICE, date.fromisoformat(DEMO_DATE), SHIFT_TYPE, mode=MODE
    )
    if session.live:
        await _go_live(session)
    log.info(
        "shift %s started in %s mode at %s", session.key, session.mode, session.replay.now
    )


app = FastAPI(
    title="Shift Readiness Agent",
    description="Commute delays, translated into floor readiness for a line manager.",
    version="0.3.0",
    lifespan=lifespan,
)

# The board is served from a separate dev server during development.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# --------------------------------------------------------------- tenant scope


class Scope:
    """The tenant, site and shift every request is answered within.

    A FastAPI dependency rather than four repeated parameters, so there is one
    place where the scoping rule lives and no endpoint can quietly forget it.
    Defaults come from config, which is what lets the demo be argument-free
    without making the isolation optional.
    """

    def __init__(
        self,
        business_unit: str = Query(BUSINESS_UNIT, description="Client account"),
        office: str = Query(OFFICE, description="Site"),
        shift_date: date = Query(
            default_factory=lambda: date.fromisoformat(DEMO_DATE), description="Shift date"
        ),
        shift_type: str = Query(SHIFT_TYPE, description="Shift start, HH:MM"),
    ) -> None:
        self.business_unit = business_unit
        self.office = office
        self.shift_date = shift_date
        self.shift_type = shift_type

    def session(self, create: bool = True) -> Session:
        """The running replay for this scope, as an HTTP-shaped error if absent."""
        try:
            session = get_session(
                self.business_unit, self.office, self.shift_date, self.shift_type, create
            )
        except RosterMissing as exc:
            raise HTTPException(404, str(exc)) from exc
        except SessionMissing as exc:
            raise HTTPException(404, str(exc)) from exc
        # Serverless hosts drop the background ticker when the request ends.
        # Catch up from wall time on the next read so "Run the morning" still moves.
        if session.running and session.started_wall is not None:
            target = min(session.target_clock(), session.replay.clock_end)
            if target > session.replay.now:
                session.replay.seek(target)
                session.persist()
        return session

    def as_dict(self) -> dict[str, Any]:
        return {
            "business_unit": self.business_unit,
            "office": self.office,
            "shift_date": self.shift_date.isoformat(),
            "shift_type": self.shift_type,
        }


# ------------------------------------------------------------------- control


@app.post("/replay/start", tags=["replay"])
async def start_replay(
    speed: float = Query(60.0, gt=0, le=3600, description="Replay minutes per real second"),
    to: str | None = Query(None, description="Jump straight to HH:MM instead of running"),
    scope: Scope = Depends(),
) -> dict[str, Any]:
    """Start the clock, or jump it to a given time.

    `to` exists for the demo and for tests: a presenter who wants to open on
    the worst moment should not have to wait two minutes for the clock to get
    there.
    """
    session = scope.session()
    session.speed = speed

    if to:
        target = datetime.combine(scope.shift_date, datetime.strptime(to, "%H:%M").time())
        if target < session.replay.now:
            # The clock only moves forward, so rewinding means starting over.
            session = _recreate(session)
        # Pause first. A live ticker (or Vercel catch-up from an old
        # started_wall) would otherwise walk past the landing on the next read.
        session.running = False
        session.started_wall = None
        session.started_clock = None
        if session.task and not session.task.done():
            session.task.cancel()
            try:
                await session.task
            except asyncio.CancelledError:
                pass
        session.task = None
        session.replay.seek(target)
        session.persist()
        # A jump lands on a situation worth explaining as often as the clock
        # does. Without this, "Jump to 08:55" showed the grey fallback until
        # somebody pressed Resume.
        await _narrate_pending(session)
        return {"status": "positioned", "clock": session.replay.now.isoformat()}

    if session.running:
        return {"status": "already running", "clock": session.replay.now.isoformat()}

    session.running = True
    session.started_wall = datetime.now()
    session.started_clock = session.replay.now
    session.task = asyncio.create_task(_run(session))
    session.persist()
    return {"status": "running", "speed": speed, "clock": session.replay.now.isoformat()}


async def _run(session: Session) -> None:
    """Advance the clock in the background, narrating what deserves it.

    Where the clock should be is derived from elapsed wall time rather than
    accumulated sleeps, so a tick held up by a slow write or a busy event loop
    is caught up instead of lost. At 60x that distinction is cosmetic. In live
    mode it is the difference between a clock and an approximation of one, and a
    board that drifts a minute behind the floor every few minutes is worse than
    no board at all.
    """
    try:
        while session.replay.now < session.replay.clock_end:
            target = min(session.target_clock(), session.replay.clock_end)
            if target > session.replay.now:
                session.replay.seek(target)
                session.persist()
                await _narrate_pending(session)
            # Poll faster than a tick, but never slower than once a second, so
            # a live clock lands on the minute rather than up to a minute late.
            await asyncio.sleep(min(1.0, session.replay.tick_minutes / session.speed))
    except asyncio.CancelledError:
        pass
    finally:
        session.running = False


async def _go_live(session: Session) -> Session:
    """Run this shift's clock at real time from wherever the wall clock says.

    This is the switch a deployment ships with rather than a demo control: one
    shift-minute per wall-clock minute, nobody pressing anything. Nothing
    downstream changes, because nothing downstream reads anything but `now`.
    """
    session.mode = "live"
    session.speed = LIVE_SPEED
    anchor = live_start(session.replay)
    if anchor > session.replay.now:
        session.replay.seek(anchor)
        session.persist()
        await _narrate_pending(session)
    if not session.running:
        session.running = True
        session.started_wall = datetime.now()
        session.started_clock = session.replay.now
        session.task = asyncio.create_task(_run(session))
    return session


_narrating: set[str] = set()


async def _narrate_pending(session: Session) -> None:
    """Ask the agent to write up any alert whose situation has changed.

    The decision about *whether* to write is made upstream in `Alert`, which
    tracks what it last said and refuses to repeat itself. This only acts on
    that decision.

    Each write-up runs as its own task rather than inline, because a model call
    takes around nine seconds and the clock must not stop for it. Inline, a
    replay at 60x froze visibly every time an alert changed, which on a bad
    morning is a dozen freezes. Detached, the board keeps moving and the prose
    lands when it lands; the structured payload is on screen in the meantime.

    A per-alert guard stops a second task starting while the first is still
    writing, which would otherwise happen on the very next tick.
    """
    from agent import runner as agent_runner

    for alert in list(session.replay.alerts.values()):
        if not alert.needs_narrative(session.replay.now):
            continue
        key = f"{session.key}|{alert.queue}"
        if key in _narrating:
            continue
        _narrating.add(key)

        async def write(alert=alert, key=key) -> None:
            try:
                await agent_runner.compose(session, alert)
            except Exception:  # noqa: BLE001 - the board must outlive the model
                pass
            finally:
                _narrating.discard(key)
                session.persist()

        asyncio.create_task(write())


def _recreate(session: Session) -> Session:
    """Start the morning over.

    The replay clock only moves forward, by design: every read is guarded
    against `now` so nothing can leak in before it happened. Rewinding
    therefore means building a fresh replay rather than winding the existing
    one back, which would leave rider states and alert history from a future
    the new clock has not reached.
    """
    drop_session(
        session.replay.business_unit,
        session.replay.office,
        session.replay.shift_date,
        session.replay.shift_type,
    )
    fresh = get_session(
        session.replay.business_unit,
        session.replay.office,
        session.replay.shift_date,
        session.replay.shift_type,
        restore=False,
    )
    fresh.speed = session.speed
    return fresh


@app.post("/replay/live", tags=["replay"])
async def go_live(scope: Scope = Depends()) -> dict[str, Any]:
    """Switch this shift to a real-time clock and leave it running.

    Replay and live differ in one thing only, which is where `now` comes from.
    Everything that reads the shift is guarded by `now` and cannot tell the
    difference, so this is the same seam a consumer of real trip events would
    occupy. What is genuinely live here is the clock, the reasoning and the
    narration; the trips themselves are still the dataset's.
    """
    session = await _go_live(scope.session())
    return {
        "status": "live",
        "mode": session.mode,
        "clock": session.replay.now.isoformat(),
        "rate": LIVE_RATE,
        "ends": session.replay.clock_end.isoformat(),
    }


@app.post("/replay/pause", tags=["replay"])
async def pause_replay(scope: Scope = Depends()) -> dict[str, Any]:
    """Stop the clock where it is. Resume with /replay/start.

    The session and everything it holds survive; only the background task is
    cancelled. Starting again picks up from the same tick, because `_run`
    skips every tick at or before the current clock.
    """
    session = scope.session(create=False)
    if session.task and not session.task.done():
        session.task.cancel()
        try:
            await session.task
        except asyncio.CancelledError:
            pass
    session.running = False
    session.started_wall = None
    session.started_clock = None
    session.task = None
    session.persist()
    return {"status": "paused", "clock": session.replay.now.isoformat()}


@app.post("/replay/reset", tags=["replay"])
async def reset_replay(
    clear_cover: bool = Query(False, description="Also reset the cover-fairness counter"),
    scope: Scope = Depends(),
) -> dict[str, Any]:
    """Rewind to the start of the morning and clear this shift's record."""
    from agent import runner as agent_runner

    dropped = drop_session(
        scope.business_unit, scope.office, scope.shift_date, scope.shift_type
    )
    if dropped:
        # Clear the agent's transcripts too. Leaving them behind is how a demo
        # run twice costs three times as much the second time.
        await agent_runner.clear_conversations(dropped)

    removed = store.reset_shift(scope.office, scope.shift_date, scope.shift_type)
    if clear_cover:
        store.clear_cover_log(scope.shift_date)

    fresh = scope.session()
    return {
        "status": "reset",
        "alerts_cleared": removed,
        "clock": fresh.replay.now.isoformat(),
    }


@app.post("/replay/step", tags=["replay"])
async def step_replay(
    minutes: int = Query(5, ge=1, le=180),
    scope: Scope = Depends(),
) -> dict[str, Any]:
    """Advance the clock by hand. Useful when presenting."""
    session = scope.session()
    session.replay.seek(session.replay.now + timedelta(minutes=minutes))
    session.persist()
    return {"status": "stepped", "clock": session.replay.now.isoformat()}


# ---------------------------------------------------------------------- views


@app.get("/board", tags=["board"])
async def board(scope: Scope = Depends()) -> dict[str, Any]:
    """The shift board: clock, queues, impact, and every rider's position."""
    session = scope.session()
    payload = session.replay.board()
    payload["running"] = session.running
    payload["speed"] = session.speed
    payload["mode"] = session.mode
    return payload


@app.get("/alerts", tags=["board"])
async def alerts(scope: Scope = Depends()) -> dict[str, Any]:
    """Open and resolved alerts, with whatever has already been done about them."""
    session = scope.session()
    session.persist()
    ids = list(session.alert_ids.values())
    taken = store.actions_for(ids)

    out = []
    for queue, alert in session.replay.alerts.items():
        alert_id = session.alert_ids.get(queue)
        out.append(
            alert.as_dict()
            | {
                "id": alert_id,
                "actions": [
                    {
                        "pathway": a["pathway"],
                        "draft": a["draft"],
                        "people": a["candidates"],
                        "cost": a["cost"],
                        "sent_at": a["sent_at"].isoformat(),
                        "time": a["sent_at"].strftime("%H:%M"),
                    }
                    for a in taken.get(alert_id or -1, [])
                ],
            }
        )
    out.sort(key=lambda a: (a["status"] == "RESOLVED", a["opened_at"]))
    return {"clock": session.replay.now.isoformat(), "alerts": out}


@app.get("/events", tags=["board"])
async def events(
    since: str | None = Query(None), scope: Scope = Depends()
) -> dict[str, Any]:
    """The feed. `since` is an ISO timestamp from a previous response."""
    session = scope.session()
    cutoff = datetime.fromisoformat(since) if since else None
    feed = [
        e.as_dict()
        for e in session.replay.events
        if cutoff is None or e.at > cutoff
    ]
    return {"clock": session.replay.now.isoformat(), "events": feed}


# --------------------------------------------------------------------- acting


@app.post("/alerts/{alert_id}/act", tags=["act"])
async def act(
    alert_id: int,
    pathway: str = Query(..., description="Which option the manager chose"),
    at: str | None = Query(None, description="HH:MM, when acting from the story slider"),
    scope: Scope = Depends(),
) -> dict[str, Any]:
    """Record a decision, and charge its cost to whoever absorbs it.

    This is where the system stops describing and starts doing. The draft it
    returns is the message the manager sends; the row it writes is what lets
    tomorrow's alert know who covered today.
    """
    session = scope.session()
    session.persist()
    found = session.alert_for(alert_id)
    if found is None:
        raise HTTPException(404, f"alert {alert_id} is not part of this shift")
    _, alert = found

    # Acting from the story slider: use the alert as it stood at that minute,
    # so the options offered and the draft written match what is on screen.
    when = session.replay.now
    if at and session.timeline and session.timeline.ready:
        snap = session.timeline.at(at)
        if snap:
            when = datetime.fromisoformat(snap["clock"])
            frozen = next((a for a in snap["alerts"] if a.get("id") == alert_id), None)
            if frozen:
                from app.core.alerts import Option
                alert.options = [
                    Option(
                        pathway=Pathway(o["pathway"]),
                        label=o["label"],
                        rationale=o["rationale"],
                        people=o["people"],
                        cost=o["cost"],
                        recommended=o["recommended"],
                    )
                    for o in frozen["options"]
                ]
                alert.drafts = frozen.get("drafts") or {}
                alert.impact = frozen.get("impact") or alert.impact
                alert.coverage_pct = frozen.get("coverage_pct", alert.coverage_pct)

    try:
        chosen = Pathway(pathway)
    except ValueError:
        raise HTTPException(400, f"unknown pathway {pathway!r}")

    option = next((o for o in alert.options if o.pathway is chosen), None)
    if option is None:
        offered = [o.pathway.value for o in alert.options]
        raise HTTPException(
            409, f"{pathway} is not currently offered on this alert; options are {offered}"
        )

    draft = alert.drafts.get(chosen.value) or _fallback_draft(alert, option)

    # Cover minutes are charged only to people actually moved onto the queue.
    # Escalations and phone calls cost somebody's attention, not their shift.
    if chosen in {Pathway.EARLY_SHIFT_COVER, Pathway.CROSS_COVER}:
        movers = [p["stwid"] for p in option.people if p.get("stwid")]
        minutes = round((alert.impact.get("minutes_lost") or 0) / max(1, len(movers)))
        if movers:
            record_cover(movers, scope.shift_date, minutes)

    action = store.record_action(
        alert_id=alert_id,
        pathway=chosen.value,
        draft=draft,
        people=option.people,
        at=when,
        cost=option.cost,
    )
    return {
        "status": "recorded",
        "pathway": chosen.value,
        "draft": draft,
        "people": option.people,
        "sent_at": action["sent_at"].isoformat(),
    }


def _fallback_draft(alert: Alert, option) -> str:
    """A usable message when no model has written one.

    The board must never show an empty action. If the narrative layer is down
    or has not run for this situation yet, the structured payload is still
    enough to compose something a manager could actually send.
    """
    names = ", ".join(
        str(p.get("name") or p.get("vendor") or p.get("role")) for p in option.people
    ) or "the team"
    when = alert.updated_at.strftime("%H:%M")

    if option.pathway is Pathway.EARLY_SHIFT_COVER:
        return (
            f"{alert.display_name} is {alert.coverage_pct}% staffed for the "
            f"{alert.shift_type} start. Could {names} move onto the queue until "
            f"the {alert.shift_type} team is in? Asking at {when}."
        )
    if option.pathway is Pathway.CROSS_COVER:
        return (
            f"{alert.display_name} is short and its own cover pool is exhausted. "
            f"Requesting {names} from the adjacent queue, accepting slower handling."
        )
    if option.pathway is Pathway.HOLD_OVER:
        cost = option.cost or {}
        return (
            f"{names}: please hold your positions past shift end. "
            f"{cost.get('summary', '')}. Relief is en route."
        )
    if option.pathway is Pathway.CONTACT_EMPLOYEE:
        return (
            f"No pickup recorded for {names} on the {alert.shift_type} run. "
            f"Please confirm whether they are travelling."
        )
    if option.pathway is Pathway.ESCALATE_TRANSPORT:
        return (
            f"{alert.display_name} at {alert.office}: several riders affected on "
            f"{names} this morning. Raising for the {alert.shift_date} record."
        )
    if option.pathway is Pathway.ESCALATE_OPS:
        day = (alert.impact.get("day") or {}).get("headline", "")
        return (
            f"{alert.display_name}, {alert.shift_date}: {day}. "
            f"Cause: {alert.cause.value.replace('_', ' ').lower()}. "
            f"Flagging now rather than in the evening report."
        )
    return f"{alert.display_name}: holding at {alert.coverage_pct}% and watching."


# ---------------------------------------------------------------------- chat


# ------------------------------------------------------------------ story


@app.post("/timeline/build", tags=["story"])
async def build_timeline(
    narrate: bool = Query(True, description="Write up alerts as they open"),
    scope: Scope = Depends(),
) -> dict[str, Any]:
    """Capture the whole morning, tick by tick, so a slider can scrub it.

    Runs in the background. Poll /timeline/status. The first build narrates
    each alert as it opens and takes a couple of minutes; a rebuild hits the
    narrative memo and takes a few seconds.
    """
    from app import timeline as tl

    session = scope.session()
    if session.timeline and session.timeline.building:
        return {"status": "already building", **session.timeline.status()}
    if session.task and not session.task.done():
        session.task.cancel()
        session.running = False
    asyncio.create_task(tl.build(session, narrate=narrate))
    await asyncio.sleep(0)
    return {"status": "building", **(session.timeline.status() if session.timeline else {})}


@app.get("/timeline/status", tags=["story"])
async def timeline_status(scope: Scope = Depends()) -> dict[str, Any]:
    session = scope.session()
    if session.timeline is None:
        return {"building": False, "ready": False, "ticks_done": 0, "ticks_total": 0, "landmarks": []}
    return session.timeline.status()


@app.get("/timeline", tags=["story"])
async def timeline_at(
    at: str = Query(..., description="HH:MM"), scope: Scope = Depends()
) -> dict[str, Any]:
    """The board, alerts and feed as they were at one minute of the morning.

    Actions the presenter takes are live, not part of the capture, so they are
    merged in here and filtered to what had been sent by that minute.
    """
    session = scope.session()
    timeline = session.timeline
    if timeline is None or not timeline.ready:
        raise HTTPException(409, "the timeline has not been built; POST /timeline/build first")
    snap = timeline.at(at)
    if snap is None:
        raise HTTPException(404, f"no snapshot at {at}")

    ids = [a["id"] for a in snap["alerts"] if a.get("id")]
    taken = store.actions_for(ids)
    cutoff = snap["clock"]
    alerts = []
    for alert in snap["alerts"]:
        actions = [
            {
                "pathway": a["pathway"],
                "draft": a["draft"],
                "people": a["candidates"],
                "cost": a["cost"],
                "sent_at": a["sent_at"].isoformat(),
                "time": a["sent_at"].strftime("%H:%M"),
            }
            for a in taken.get(alert.get("id") or -1, [])
            if a["sent_at"].isoformat() <= cutoff
        ]
        alerts.append(alert | {"actions": actions})
    return {
        "clock": snap["clock"],
        "time": snap["time"],
        "board": snap["board"],
        "alerts": alerts,
        "events": snap["events"],
        "landmarks": [m.as_dict() for m in timeline.landmarks],
    }


@app.post("/chat", tags=["agent"])
async def chat(
    request: Request,
    body: dict = Body(...),
    stream: bool = Query(False),
    scope: Scope = Depends(),
):
    """Ask the agent about the shift.

    Chat keeps a persistent per-shift conversation. Pass stream=1 (or Accept
    text/event-stream) to receive tokens as they are generated.
    """
    from agent import runner as agent_runner

    text = (body or {}).get("text", "").strip()
    if not text:
        raise HTTPException(400, "send a question as {'text': ...}")

    session = scope.session()
    session.persist()
    clock = session.replay.now.isoformat()
    wants_stream = stream or "text/event-stream" in request.headers.get("accept", "")
    if not wants_stream:
        answer = await agent_runner.ask(session, text)
        return {"clock": clock, **answer}

    async def events():
        async for item in agent_runner.ask_stream(session, text):
            yield f"data: {json.dumps({'clock': clock, **item})}\n\n"

    return StreamingResponse(events(), media_type="text/event-stream")


@app.post("/alerts/{alert_id}/narrate", tags=["agent"])
async def narrate(alert_id: int, scope: Scope = Depends()) -> dict[str, Any]:
    """Write up one alert on demand. Used by the demo and by tests."""
    from agent import runner as agent_runner

    session = scope.session()
    session.persist()
    found = session.alert_for(alert_id)
    if found is None:
        raise HTTPException(404, f"alert {alert_id} is not part of this shift")
    _, alert = found

    written = await agent_runner.compose(session, alert)
    session.persist()
    return {
        "status": "written" if written else "unavailable",
        "narrative": alert.narrative,
        "drafts": alert.drafts,
        "usage": agent_runner.USAGE.as_dict(),
    }


@app.get("/usage", tags=["agent"])
async def usage() -> dict[str, Any]:
    """What inference has cost this process. The cost claim, metered."""
    from agent import runner as agent_runner

    return agent_runner.USAGE.as_dict()


WEB = Path(__file__).resolve().parent.parent / "web" / "index.html"


@app.get("/", include_in_schema=False)
async def board_page() -> FileResponse:
    """The shift board. One file, no build step, served by the same process."""
    return FileResponse(WEB, media_type="text/html")


@app.get("/config", tags=["ops"])
async def config() -> dict[str, Any]:
    """What this deployment is pointed at, for the board to read on load.

    The board used to hard-code the site, the shift and the width of the
    morning, which made the multi-tenancy claim true of the API and false of the
    only thing anybody looks at. It asks here instead, so pointing this at
    another tenant stays a config change.
    """
    from agent.agent import MODEL, missing_credentials

    return {
        "business_unit": BUSINESS_UNIT,
        "office": OFFICE,
        "shift_type": SHIFT_TYPE,
        "shift_date": DEMO_DATE,
        "mode": MODE,
        "clock_start": CLOCK_START,
        "clock_end": CLOCK_END,
        "live_rate": LIVE_RATE,
        "jump_to": JUMP_TO,
        "model": MODEL,
        "model_ready": missing_credentials() is None,
    }


@app.get("/health", tags=["ops"])
async def health() -> dict[str, Any]:
    """Whether this process can do its job, including the part that needs a key.

    The narrative layer degrades silently by design: if the model cannot be
    reached the board still renders every computed number, which is correct
    behaviour and indistinguishable from success at a glance. So the one thing
    worth reporting loudly here is whether the model is actually reachable.
    """
    from agent.agent import MODEL, missing_credentials

    missing = missing_credentials()
    return {
        "status": "ok",
        "mode": MODE,
        "model": MODEL,
        "model_ready": missing is None,
        "model_error": missing,
        "sessions": [
            {
                "key": k,
                "clock": s.replay.now.isoformat(),
                "running": s.running,
                "mode": s.mode,
            }
            for k, s in SESSIONS.items()
        ],
    }

