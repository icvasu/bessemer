"""Service level: the number the line manager is actually measured on.

Everything upstream of this module counts people. This is where headcount
becomes the metric that appears on the manager's scorecard and in the report
they send their own director at the end of the day.

## Why "calls unanswered" was not enough

An earlier version priced a shortfall in calls not answered. That is a fair
proxy for the size of a gap but it is linear, and staffing is not. A contact
centre queue is a waiting-line system: as agents are removed, the delay does
not rise in proportion, it rises steeply and then the queue stops coping
altogether. Losing four of twelve agents does not cost a third of the service
level. On a well-loaded queue it can cost nearly all of it.

That non-linearity is the single most useful thing this system can tell a
manager at ten to nine, because it is precisely the intuition that a headcount
number fails to convey.

## The model

Erlang C, the standard waiting-line model for inbound queues. Its assumptions
are strong and worth stating: arrivals are Poisson, handle times are
exponential, nobody abandons, and every agent can take every call. Real
workforce planning tools relax all four. None of that changes the shape of the
answer, which is what a decision at ten to nine depends on.

Written with the numerically stable Erlang B recursion rather than the textbook
factorial form, which overflows above about twenty agents.

## Reading the output

`service_level` is the fraction of calls answered inside the target, so a
target written "80/20" means 80% answered within 20 seconds. When offered load
exceeds capacity the queue is unstable: the model reports a service level near
zero and a backlog that grows for as long as the shortfall lasts. That backlog
is the reason a twenty-minute staffing gap is not a twenty-minute problem.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

DEFAULT_TARGET_PCT = 80.0
DEFAULT_TARGET_SECONDS = 20.0

AVERAGE_PATIENCE_SECONDS = 90.0
"""How long a caller waits before hanging up. Used only for the abandonment
estimate, which is the crudest number here and is labelled as such."""


def parse_target(sl_target: str | None) -> tuple[float, float]:
    """Turn an '80/20' style target into (percent, seconds).

    Falls back to the industry default rather than raising, because a missing
    target should not take the whole projection down.
    """
    if not sl_target:
        return DEFAULT_TARGET_PCT, DEFAULT_TARGET_SECONDS
    try:
        pct, seconds = sl_target.split("/", 1)
        return float(pct), float(seconds)
    except (ValueError, AttributeError):
        return DEFAULT_TARGET_PCT, DEFAULT_TARGET_SECONDS


def erlang_b(agents: int, load: float) -> float:
    """Blocking probability, by the stable recursion.

    B(0) = 1;  B(n) = A·B(n-1) / (n + A·B(n-1))

    Used only as the route to Erlang C. The direct factorial form overflows
    around twenty agents, which is well inside the range this system needs.
    """
    result = 1.0
    for n in range(1, agents + 1):
        result = (load * result) / (n + load * result)
    return result


def erlang_c(agents: int, load: float) -> float:
    """Probability that an arriving call has to wait at all."""
    if agents <= 0:
        return 1.0
    if load <= 0:
        return 0.0
    occupancy = load / agents
    if occupancy >= 1.0:
        # Arrivals outrun capacity. Every call waits, and the wait grows
        # without bound for as long as that holds.
        return 1.0
    blocking = erlang_b(agents, load)
    denominator = 1.0 - occupancy * (1.0 - blocking)
    if denominator <= 0:
        return 1.0
    return blocking / denominator


@dataclass(frozen=True)
class ServiceLevel:
    """One queue's service level for one interval, at a given headcount."""

    agents: float
    offered_load: float
    """Erlangs: the agent-time the arriving calls demand."""

    occupancy: float
    """Load per agent. At or above 1.0 the queue cannot keep up."""

    prob_wait: float
    service_level: float
    """Fraction answered inside the target. The headline."""

    asa_seconds: float
    """Average speed of answer."""

    abandon_rate: float
    backlog_calls: float
    """Calls accumulating during this interval because capacity is short.
    Zero unless the queue is overloaded."""

    target_pct: float
    target_seconds: float

    @property
    def is_overloaded(self) -> bool:
        return self.occupancy >= 1.0

    @property
    def meets_target(self) -> bool:
        return self.service_level * 100 >= self.target_pct

    @property
    def headline(self) -> str:
        """One phrase a manager can read at a glance."""
        if self.agents <= 0:
            return "queue unmanned"
        if self.is_overloaded:
            return (
                f"over capacity at {self.agents:.0f} agents, "
                f"{self.backlog_calls:.0f} calls backing up"
            )
        return (
            f"{self.service_level * 100:.0f}% answered in {self.target_seconds:.0f}s "
            f"against a {self.target_pct:.0f}% target"
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "agents": round(self.agents, 1),
            "offered_load": round(self.offered_load, 2),
            "occupancy": round(self.occupancy, 3),
            "occupancy_pct": round(self.occupancy * 100),
            "service_level_pct": round(self.service_level * 100),
            "target_pct": round(self.target_pct),
            "target_seconds": round(self.target_seconds),
            "meets_target": self.meets_target,
            "asa_seconds": round(self.asa_seconds),
            "abandon_pct": round(self.abandon_rate * 100, 1),
            "backlog_calls": round(self.backlog_calls),
            "overloaded": self.is_overloaded,
            "headline": self.headline,
        }


def service_level(
    agents: float,
    calls_per_interval: float,
    aht_min: float,
    interval_min: float = 30.0,
    sl_target: str | None = None,
) -> ServiceLevel:
    """Project service level for one interval at a given headcount.

    Args:
        agents: bodies on the queue. Fractional values are floored, since half
            an agent answers no calls.
        calls_per_interval: forecast arrivals in the interval.
        aht_min: average handle time in minutes.
        interval_min: length of the interval.
        sl_target: target written as 'pct/seconds', e.g. '80/20'.
    """
    target_pct, target_seconds = parse_target(sl_target)
    whole_agents = int(max(0, math.floor(agents)))

    arrival_rate = calls_per_interval / interval_min if interval_min else 0.0
    load = arrival_rate * aht_min

    if whole_agents <= 0:
        return ServiceLevel(
            agents=0, offered_load=load, occupancy=float("inf"), prob_wait=1.0,
            service_level=0.0, asa_seconds=float(interval_min * 60),
            abandon_rate=1.0, backlog_calls=calls_per_interval,
            target_pct=target_pct, target_seconds=target_seconds,
        )

    occupancy = load / whole_agents
    prob_wait = erlang_c(whole_agents, load)

    if occupancy >= 1.0:
        # Unstable. Service level collapses and the excess simply accumulates.
        # Reporting a small positive number here would be worse than useless:
        # it would suggest the queue is coping when the backlog is compounding.
        excess_per_min = arrival_rate - (whole_agents / aht_min if aht_min else 0)
        return ServiceLevel(
            agents=whole_agents, offered_load=load, occupancy=occupancy,
            prob_wait=1.0, service_level=0.0,
            asa_seconds=float(interval_min * 60), abandon_rate=0.9,
            backlog_calls=max(0.0, excess_per_min * interval_min),
            target_pct=target_pct, target_seconds=target_seconds,
        )

    # Standard Erlang C results.
    spare = whole_agents - load
    sl = 1.0 - prob_wait * math.exp(-spare * (target_seconds / 60.0) / aht_min)
    asa = (prob_wait * aht_min * 60.0) / spare if spare > 0 else float(interval_min * 60)

    # Abandonment is a rough overlay, not part of Erlang C. Callers are assumed
    # to give up on an exponential patience curve, so it tracks the answer
    # delay without pretending to precision.
    abandon = prob_wait * (1.0 - math.exp(-asa / AVERAGE_PATIENCE_SECONDS))

    return ServiceLevel(
        agents=whole_agents, offered_load=load, occupancy=occupancy,
        prob_wait=prob_wait, service_level=max(0.0, min(1.0, sl)),
        asa_seconds=asa, abandon_rate=abandon, backlog_calls=0.0,
        target_pct=target_pct, target_seconds=target_seconds,
    )


def agents_needed(
    calls_per_interval: float,
    aht_min: float,
    interval_min: float = 30.0,
    sl_target: str | None = None,
    ceiling: int = 200,
) -> int:
    """Smallest headcount that meets the target. The staffing answer.

    Turns the alert from a warning into an instruction: not "you are short"
    but "you need two more on this queue".
    """
    for n in range(1, ceiling + 1):
        if service_level(n, calls_per_interval, aht_min, interval_min, sl_target).meets_target:
            return n
    return ceiling


@dataclass(frozen=True)
class DayProjection:
    """The whole shift's service level, which is what gets reported upward.

    A manager is not judged on the nine o'clock half-hour. They are judged on
    the day. This rolls the breached intervals in with the healthy ones so the
    alert can say whether the day is still recoverable, which is the difference
    between an incident and a note in the log.
    """

    intervals_total: int
    intervals_breached: int
    day_service_level: float
    target_pct: float
    calls_total: float
    calls_answered_late: float
    backlog_calls: float
    recoverable: bool

    @property
    def meets_target(self) -> bool:
        return self.day_service_level * 100 >= self.target_pct

    @property
    def headline(self) -> str:
        verdict = "holds" if self.meets_target else "misses"
        return (
            f"day service level {self.day_service_level * 100:.0f}% "
            f"{verdict} the {self.target_pct:.0f}% target"
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "intervals_total": self.intervals_total,
            "intervals_breached": self.intervals_breached,
            "day_service_level_pct": round(self.day_service_level * 100),
            "target_pct": round(self.target_pct),
            "meets_target": self.meets_target,
            "calls_total": round(self.calls_total),
            "calls_answered_late": round(self.calls_answered_late),
            "backlog_calls": round(self.backlog_calls),
            "recoverable": self.recoverable,
            "headline": self.headline,
        }


@dataclass(frozen=True)
class Adherence:
    """Schedule adherence: rostered time the team was actually available.

    The simplest metric here and the one already on the manager's scorecard.
    Twenty minutes late is twenty minutes of non-adherence, with no modelling
    in between. It is also the only one of these numbers that is a fact rather
    than a projection once the morning is over, which makes it the right thing
    to put in a report sent upward.
    """

    rostered: int
    minutes_scheduled: float
    minutes_lost: float
    worst_case_minutes_lost: float

    @property
    def adherence(self) -> float:
        if self.minutes_scheduled <= 0:
            return 1.0
        return max(0.0, 1.0 - self.minutes_lost / self.minutes_scheduled)

    @property
    def headline(self) -> str:
        return (
            f"{self.adherence * 100:.1f}% schedule adherence, "
            f"{self.minutes_lost:.0f} agent-minutes lost"
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "rostered": self.rostered,
            "minutes_scheduled": round(self.minutes_scheduled),
            "minutes_lost": round(self.minutes_lost),
            "worst_case_minutes_lost": round(self.worst_case_minutes_lost),
            "adherence_pct": round(self.adherence * 100, 1),
            "headline": self.headline,
        }


def adherence(
    rostered: int, minutes_lost: float, worst_case_minutes_lost: float, shift_hours: float = 8.0
) -> Adherence:
    """Adherence for a queue over the whole shift."""
    return Adherence(
        rostered=rostered,
        minutes_scheduled=rostered * shift_hours * 60,
        minutes_lost=minutes_lost,
        worst_case_minutes_lost=worst_case_minutes_lost,
    )


def project_day(
    impaired: list[tuple[float, float]],
    full_strength_agents: float,
    calls_per_interval: float,
    aht_min: float,
    shift_hours: float = 8.0,
    interval_min: float = 30.0,
    sl_target: str | None = None,
) -> DayProjection:
    """Roll a disrupted morning into the day's reported number.

    Args:
        impaired: (agents, share_of_interval) for each affected interval, in
            order. The share lets a gap that closes halfway through an interval
            be weighted honestly instead of costing the whole thirty minutes.
        full_strength_agents: headcount once everyone has arrived.
        calls_per_interval: forecast arrivals per interval.
        aht_min: average handle time.
        shift_hours: length of the shift.
        interval_min: interval length.
        sl_target: target as 'pct/seconds'.
    """
    target_pct, _ = parse_target(sl_target)
    total_intervals = max(1, int(round(shift_hours * 60 / interval_min)))

    healthy = service_level(
        full_strength_agents, calls_per_interval, aht_min, interval_min, sl_target
    )

    answered_in_target = 0.0
    calls_total = 0.0
    breached = 0
    backlog = 0.0

    for index in range(total_intervals):
        if index < len(impaired):
            agents, share = impaired[index]
            share = max(0.0, min(1.0, share))
            impaired_sl = service_level(
                agents, calls_per_interval, aht_min, interval_min, sl_target
            )
            # Blend the impaired and recovered portions of the interval.
            level = impaired_sl.service_level * share + healthy.service_level * (1 - share)
            backlog += impaired_sl.backlog_calls * share
            if level * 100 < target_pct:
                breached += 1
        else:
            level = healthy.service_level

        answered_in_target += calls_per_interval * level
        calls_total += calls_per_interval

    day_level = answered_in_target / calls_total if calls_total else 1.0

    # Could a perfect remainder still rescue the day? Answered against the same
    # denominator, because the calls already missed do not disappear.
    remaining = total_intervals - min(len(impaired), total_intervals)
    best_case = (answered_in_target - sum(
        calls_per_interval * healthy.service_level for _ in range(remaining)
    ) + calls_per_interval * remaining) / calls_total if calls_total else 1.0

    return DayProjection(
        intervals_total=total_intervals,
        intervals_breached=breached,
        day_service_level=day_level,
        target_pct=target_pct,
        calls_total=calls_total,
        calls_answered_late=calls_total - answered_in_target,
        backlog_calls=backlog,
        recoverable=best_case * 100 >= target_pct,
    )
