"""The morning as a story: every tick captured, ready to scrub through.

The live replay is honest but linear. It only moves forward, which is right for
a system that must never see the future, and wrong for a presenter who wants to
say "here is 08:12, and here is what it looked like twenty minutes later" and
drag between the two.

This module runs a morning once, from 07:30 to 10:00, and keeps a snapshot of
the board, the alerts and the feed at every tick. It narrates as it goes,
waiting for each write-up so the snapshot at 08:25 carries the prose that was
written at 08:25 and not a blank. Then a slider can land on any minute and show
exactly what the manager would have seen.

Two things about it are worth saying out loud.

**It is precomputed, and that is not a trick.** Every snapshot is the same
deterministic function the live clock runs, evaluated at that instant, with the
same guard against reading the future. The narratives are real model output,
produced in order. Precomputing them means the presenter is not waiting ten
seconds on stage for a sentence, and the memo means a rehearsal makes the
performance free.

**Landmarks are derived, not hand-placed.** The moments worth stopping on are
read off the event feed: the first cab that fails to leave, each alert opening,
each rider a cab passed without collecting, shift start, the end of grace, each
queue recovering, and the moment everyone is in. If the data changes, the story
changes with it.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, time as dtime
from typing import Any

from app.replay import Replay
from app.sessions import Session
from app import store

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Landmark:
    """A moment worth stopping the story on."""

    at: datetime
    label: str
    kind: str
    """`bad`, `warn`, `ok`, or `mark`. Drives the colour of the dot."""

    def as_dict(self) -> dict[str, Any]:
        return {
            "at": self.at.isoformat(),
            "time": self.at.strftime("%H:%M"),
            "label": self.label,
            "kind": self.kind,
        }


@dataclass
class Timeline:
    """One morning, captured tick by tick."""

    snapshots: dict[str, dict[str, Any]] = field(default_factory=dict)
    landmarks: list[Landmark] = field(default_factory=list)
    ticks_total: int = 0
    ticks_done: int = 0
    narrated: int = 0
    building: bool = False
    ready: bool = False
    error: str | None = None
    started_at: float = 0.0
    seconds: float = 0.0

    @property
    def keys(self) -> list[str]:
        return sorted(self.snapshots)

    def at(self, hhmm: str) -> dict[str, Any] | None:
        """The snapshot at or just before a time."""
        if not self.snapshots:
            return None
        if hhmm in self.snapshots:
            return self.snapshots[hhmm]
        earlier = [k for k in self.keys if k <= hhmm]
        return self.snapshots[earlier[-1]] if earlier else self.snapshots[self.keys[0]]

    def status(self) -> dict[str, Any]:
        return {
            "building": self.building,
            "ready": self.ready,
            "ticks_done": self.ticks_done,
            "ticks_total": self.ticks_total,
            "narrated": self.narrated,
            "seconds": round(self.seconds, 1),
            "error": self.error,
            "first": self.keys[0] if self.keys else None,
            "last": self.keys[-1] if self.keys else None,
            "landmarks": [m.as_dict() for m in self.landmarks],
        }


async def build(session: Session, narrate: bool = True) -> Timeline:
    """Run the whole morning on this session, capturing every tick.

    The session's own replay is reset and driven, so the tools the narrator
    calls see the same alerts the snapshots are taken from. When it finishes,
    the session sits at the end of the morning with every alert resolved and
    written up, which is also a perfectly good place for the live board to be.

    Narration is awaited inline. That is the opposite of what the live clock
    does, and deliberately: here nobody is watching the clock, and a snapshot
    without its prose would defeat the purpose.
    """
    from agent import runner as agent_runner

    timeline = Timeline(building=True, started_at=time.perf_counter())
    session.timeline = timeline

    replay = Replay(
        shift_date=session.replay.shift_date,
        office=session.replay.office,
        business_unit=session.replay.business_unit,
        shift_type=session.replay.shift_type,
    )
    session.replay = replay
    session.alert_ids = {}
    store.reset_shift(replay.office, replay.shift_date, replay.shift_type)

    ticks = list(replay.ticks())
    timeline.ticks_total = len(ticks)
    everyone_in: datetime | None = None

    try:
        for tick in ticks:
            replay.advance(tick)
            session.persist()

            if narrate:
                for alert in list(replay.alerts.values()):
                    if not alert.needs_narrative(tick):
                        continue
                    written = await agent_runner.compose(session, alert)
                    if written:
                        timeline.narrated += 1
                    session.persist()

            board = replay.board()
            if everyone_in is None and board["totals"]["on_floor"] == board["totals"]["rostered"]:
                everyone_in = tick

            timeline.snapshots[tick.strftime("%H:%M")] = {
                "clock": tick.isoformat(),
                "time": tick.strftime("%H:%M"),
                "board": board,
                "alerts": [
                    alert.as_dict() | {"id": session.id_for(queue)}
                    for queue, alert in replay.alerts.items()
                ],
                "events": [e.as_dict() for e in replay.events],
            }
            timeline.ticks_done += 1
            # Yield so status polls and other requests get a look in.
            await asyncio.sleep(0)

        timeline.landmarks = derive_landmarks(replay, everyone_in)
        timeline.ready = True
    except Exception as exc:  # noqa: BLE001 - report, never crash the API
        log.exception("timeline build failed")
        timeline.error = str(exc)
    finally:
        timeline.building = False
        timeline.seconds = time.perf_counter() - timeline.started_at

    return timeline


def derive_landmarks(replay: Replay, everyone_in: datetime | None) -> list[Landmark]:
    """Read the story's beats off the feed.

    Kept to the moments a presenter would actually stop on. Every individual
    arrival is in the feed; none of them is a landmark.
    """
    marks: list[Landmark] = []
    seen_cab_late = False
    seen_arrival = False

    for event in replay.events:
        if event.kind == "cab_late" and not seen_cab_late:
            marks.append(Landmark(event.at, f"First cab fails to leave: {event.subject}", "warn"))
            seen_cab_late = True
        elif event.kind == "alert_opened":
            marks.append(Landmark(event.at, f"{event.subject} alert opens", "bad"))
        elif event.kind in {"no_pickup", "no_show"}:
            marks.append(Landmark(event.at, f"{event.subject} not collected", "bad"))
        elif event.kind == "arrived" and not seen_arrival:
            marks.append(Landmark(event.at, f"First arrival: {event.subject}", "ok"))
            seen_arrival = True
        elif event.kind == "alert_resolved":
            marks.append(Landmark(event.at, f"{event.subject} back to strength", "ok"))

    marks.append(Landmark(replay.shift_start, "Shift starts", "mark"))
    marks.append(Landmark(replay.deadline, "Grace period ends", "mark"))
    if everyone_in:
        marks.append(Landmark(everyone_in, "Everyone is in", "ok"))

    # One landmark per minute, earliest wins, alerts outrank riders.
    priority = {"bad": 0, "mark": 1, "ok": 2, "warn": 3}
    by_minute: dict[str, Landmark] = {}
    for mark in sorted(marks, key=lambda m: (m.at, priority[m.kind])):
        key = mark.at.strftime("%H:%M")
        by_minute.setdefault(key, mark)
    return sorted(by_minute.values(), key=lambda m: m.at)
