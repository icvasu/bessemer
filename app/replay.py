"""The clock.

The dataset is three months of finished mornings. To demonstrate an agent that
senses and acts, those mornings have to happen again in order, with nothing
visible before it occurred. This module is that clock, and the discipline it
enforces is the reason the demo is honest: every read goes through `now`, and
`now` only moves forward.

The clock runs at two speeds and it is worth being exact about the difference.
In `replay` mode it steps a finished morning at a multiple of real time, which
is what a demo needs. In `live` mode it advances one shift-minute per wall-clock
minute, so the board moves on its own with nobody pressing anything.

Live mode is a real clock over recorded events, not a live feed, and the
distinction matters: what is genuinely live is the clock, the reasoning and the
narration, while the trips themselves are still the dataset's. Because every
read is guarded by `now` and nothing downstream can tell where `now` came from,
replacing this file with a consumer of real trip events changes the source of
the rows and nothing else. That is the deployability claim in concrete form.

Run:  uv run python -m app.replay --date 2026-06-11
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any, Callable, Iterator

from app.config import (
    BUSINESS_UNIT,
    CLOCK_END,
    CLOCK_START,
    DEMO_DATE,
    GRACE_MIN,
    OFFICE,
    SHIFT_TYPE,
)
from app.core.alerts import Alert, Status, build_alert
from app.core.queue import QueueProjection, floor_totals, project_queues
from app.core.state import State, rider_state
from app.db import query


@dataclass
class Event:
    """Something that happened, in the order it happened."""

    at: datetime
    kind: str
    subject: str
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "at": self.at.isoformat(),
            "time": self.at.strftime("%H:%M"),
            "kind": self.kind,
            "subject": self.subject,
            "detail": self.detail,
        }


# Which state transitions are worth a line in the feed. Everything else is
# noise: a rider sitting in SCHEDULED for forty minutes is not news.
NOTABLE = {
    State.CAB_MOVING: ("cab_started", "cab on the road"),
    State.CAB_LATE: ("cab_late", "cab has not left its depot"),
    State.PICKED_UP: ("picked_up", "picked up"),
    State.DROPPED: ("arrived", "on the floor"),
    State.NO_PICKUP: ("no_pickup", "not collected, cab has passed"),
    State.NO_SHOW: ("no_show", "did not travel, no notice given"),
    State.CANCELLED: ("cancelled", "cancelled in advance"),
}


@dataclass
class Replay:
    """One shift-day, replayed a tick at a time.

    The day's rows are loaded once. Everything after that is arithmetic against
    the clock, so a tick costs no database round trip and no model call. That
    is what makes it credible at enterprise volume: the expensive thing happens
    only when the situation changes enough to be worth saying out loud.
    """

    shift_date: date
    office: str = OFFICE
    business_unit: str = BUSINESS_UNIT
    shift_type: str = SHIFT_TYPE
    start: time = field(default_factory=lambda: time.fromisoformat(CLOCK_START))
    end: time = field(default_factory=lambda: time.fromisoformat(CLOCK_END))
    tick_minutes: int = 1

    legs: list[dict] = field(default_factory=list)
    queue_rows: list[dict] = field(default_factory=list)
    alerts: dict[str, Alert] = field(default_factory=dict)
    events: list[Event] = field(default_factory=list)
    _seen_state: dict[int, State] = field(default_factory=dict)
    now: datetime | None = None

    # ------------------------------------------------------------------ setup

    def __post_init__(self) -> None:
        if not self.legs:
            self.legs = query(
                """
                SELECT * FROM v_roster_day
                WHERE trip_date = %s AND office = %s AND role = 'primary'
                ORDER BY display_name
                """,
                (self.shift_date, self.office),
            )
        if not self.queue_rows:
            self.queue_rows = query(
                "SELECT * FROM queues WHERE office = %s ORDER BY queue", (self.office,)
            )
        self.now = self.clock_start

    @property
    def clock_start(self) -> datetime:
        return datetime.combine(self.shift_date, self.start)

    @property
    def clock_end(self) -> datetime:
        return datetime.combine(self.shift_date, self.end)

    @property
    def shift_start(self) -> datetime:
        return datetime.combine(self.shift_date, time.fromisoformat(self.shift_type))

    @property
    def deadline(self) -> datetime:
        """Shift start plus the grace the site actually operates."""
        return self.shift_start + timedelta(minutes=GRACE_MIN)

    # ------------------------------------------------------------------ ticks

    def projections(self, now: datetime | None = None) -> dict[str, QueueProjection]:
        return project_queues(
            self.legs, self.queue_rows, now or self.now, self.office, self.deadline
        )

    def advance(self, now: datetime) -> list[Event]:
        """Move the clock to `now` and return everything new since the last tick."""
        self.now = now
        fresh: list[Event] = []

        for leg in self.legs:
            state = rider_state(leg, now)
            if self._seen_state.get(leg["stwid"]) == state:
                continue
            self._seen_state[leg["stwid"]] = state
            if state not in NOTABLE:
                continue
            kind, phrase = NOTABLE[state]
            fresh.append(
                Event(at=now, kind=kind, subject=leg["display_name"], detail=phrase)
            )

        for queue, projection in self.projections(now).items():
            before = self.alerts.get(queue)
            was = before.status if before else None
            alert = build_alert(
                projection,
                now,
                self.shift_date,
                self.office,
                self.business_unit,
                self.shift_type,
                self.shift_start,
                before,
            )
            if alert is None:
                continue
            self.alerts[queue] = alert
            if was is None:
                fresh.append(
                    Event(
                        at=now,
                        kind="alert_opened",
                        subject=alert.display_name,
                        detail=(
                            f"{alert.coverage_pct}% coverage projected, "
                            f"{alert.impact.get('calls_range')} calls at risk"
                        ),
                    )
                )
            elif was is not Status.RESOLVED and alert.status is Status.RESOLVED:
                fresh.append(
                    Event(
                        at=now,
                        kind="alert_resolved",
                        subject=alert.display_name,
                        detail=f"back to {alert.coverage_pct}% strength",
                    )
                )

        self.events.extend(fresh)
        return fresh

    def seek(self, target: datetime) -> list[Event]:
        """Advance tick by tick up to `target`, returning everything new.

        Stepping rather than jumping is the whole point. Alert lifecycle
        depends on what was true a tick ago, so a clock set straight to 08:55
        would produce alerts with no history and no hysteresis, and a feed with
        no morning in it.
        """
        fresh: list[Event] = []
        for tick in self.ticks():
            if tick <= self.now:
                continue
            if tick > target:
                break
            fresh.extend(self.advance(tick))
        return fresh

    def run(self, on_tick: Callable[[datetime, list[Event]], None] | None = None) -> None:
        """Replay the whole window."""
        for now in self.ticks():
            fresh = self.advance(now)
            if on_tick:
                on_tick(now, fresh)

    def ticks(self) -> Iterator[datetime]:
        now = self.clock_start
        step = timedelta(minutes=self.tick_minutes)
        while now <= self.clock_end:
            yield now
            now += step

    # ------------------------------------------------------------------ views

    def board(self) -> dict[str, Any]:
        """Everything the shift board needs for the current instant."""
        projections = self.projections()
        return {
            "clock": self.now.isoformat(),
            "time": self.now.strftime("%H:%M"),
            "shift_date": self.shift_date.isoformat(),
            "shift_type": self.shift_type,
            "shift_start": self.shift_start.isoformat(),
            "deadline": self.deadline.isoformat(),
            "office": self.office,
            "business_unit": self.business_unit,
            "totals": floor_totals(projections),
            "queues": [p.as_dict() for p in projections.values()],
        }

    def open_alerts(self) -> list[Alert]:
        return [a for a in self.alerts.values() if a.status is not Status.RESOLVED]


# ----------------------------------------------------------------------- cli


def _print_tick(replay: Replay, now: datetime, fresh: list[Event]) -> None:
    projections = replay.projections(now)
    cells = []
    for projection in projections.values():
        impact = projection.impact()
        sl = impact.service_level
        cells.append(
            f"{projection.queue[:4]}: {len(projection.on_floor):>2}/{projection.rostered} "
            f"{projection.coverage * 100:>3.0f}%  SL {sl.service_level * 100:>3.0f}%"
            + ("!" if sl and not sl.meets_target else " ")
        )
    print(f"{now:%H:%M}  " + " | ".join(cells))
    for event in fresh:
        marker = "!" if event.kind.startswith("alert") else " "
        print(f"       {marker} {event.subject}: {event.detail}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", default=DEMO_DATE)
    parser.add_argument("--office", default=OFFICE)
    parser.add_argument("--shift", default=SHIFT_TYPE)
    parser.add_argument("--tick", type=int, default=5, help="minutes per tick")
    parser.add_argument("--quiet", action="store_true", help="only print alerts")
    parser.add_argument("--json", action="store_true", help="dump the final alerts as JSON")
    args = parser.parse_args()

    replay = Replay(
        shift_date=date.fromisoformat(args.date),
        office=args.office,
        shift_type=args.shift,
        tick_minutes=args.tick,
    )
    if not replay.legs:
        print(f"no roster rows for {args.office} on {args.date}")
        return 1

    print(
        f"{args.office}  {args.shift} shift  {args.date}  "
        f"{len(replay.legs)} rostered  deadline {replay.deadline:%H:%M}\n"
    )
    replay.run(lambda now, fresh: None if args.quiet else _print_tick(replay, now, fresh))

    print("\n--- alerts ---")
    for alert in replay.alerts.values():
        window = f"{alert.opened_at:%H:%M}"
        if alert.resolved_at:
            window += f" to {alert.resolved_at:%H:%M}"
        print(f"\n{alert.display_name}  [{alert.status.value}]  {window}  cause={alert.cause.value}")
        impact = alert.impact
        sl = impact.get("service_level") or {}
        day = impact.get("day") or {}
        print(
            f"    service level: {sl.get('service_level_pct')}% at {impact.get('coverage_pct')}% "
            f"coverage, {impact.get('service_level_full', {}).get('service_level_pct')}% at full strength"
        )
        print(f"    day: {day.get('headline')}  (needs {impact.get('agents_needed')} agents)")
        for trigger in alert.triggers:
            print(f"    trigger: {trigger}")
        for fact in alert.facts[:2]:
            print(f"    context: {fact.text}")
        for option in alert.options:
            star = "->" if option.recommended else "  "
            names = ", ".join(
                str(p.get("name") or p.get("vendor") or p.get("role"))
                for p in option.people[:3]
            )
            outcome = f"  -> {option.outcome}" if option.outcome else ""
            print(f"    {star} {option.pathway.value}{': ' + names if names else ''}{outcome}")

    if args.json:
        print("\n--- payload ---")
        print(json.dumps([a.as_dict() for a in replay.alerts.values()], indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
