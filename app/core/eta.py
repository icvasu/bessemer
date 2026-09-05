"""Arrival projection, and an honest account of how well it works.

The hard constraint: the dataset has **no GPS traces**, only pickup and drop
timestamps. Once a rider is aboard, nothing is observable until they arrive.
An en-route delay is invisible in real time.

## What the data supports

Measured on the rostered team's 1,413 completed legs, projecting at the moment
of pickup:

| Question                                   | Answer |
|--------------------------------------------|--------|
| Correlation of projected with actual lateness | 0.52 |
| AUC for "materially late" (>15 min)        | 0.64   |
| Precision at that threshold                | 0.73   |
| Recall at that threshold                   | 0.14   |
| Residual spread after pickup (sd)          | 11.7 min |

Read that honestly: at pickup time we can name *some* riders who will be badly
late, and we are usually right when we do, but we miss most of them. Eleven
minutes of standard deviation is simply not knowable without a live position
feed.

## What that means for the design

**The point estimate uses the median overshoot, not the 75th percentile.**
An early draft padded every ETA to p75. It flagged 84% of riders as late when
only 43% were, so precision fell to 0.46. A projection that cries wolf is worse
than none, because the manager stops reading it. With the median, the ETA is
unbiased: median error is -0.2 minutes.

**Uncertainty is shown, not hidden.** `Projection.spread_min` carries the
bucket's p10-to-p90 half-width so the UI can render "09:05, give or take 12"
rather than a false precision.

**The alert does not depend on the projection.** The trigger that actually
fires is deterministic: the planned drop has passed and no drop is recorded.
That has no false positives at all. It buys 5 to 15 minutes of warning, which
is enough to act on. The projection's job is the softer one of saying "watch
these three" before that moment arrives, and it is labelled as advisory.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from functools import lru_cache
from typing import Any, Mapping

from app.core.state import _at, _observed, State, cab_delay_min, rider_state
from app.db import query

# Fallbacks for buckets with too little history. Only reached at odd hours.
DEFAULT_OVERSHOOT_MIN = 2.5
DEFAULT_SPREAD_MIN = 13.0
DEFAULT_TRAVEL_MIN = 45.0

# Observations a bucket needs before its percentiles mean anything.
MIN_BUCKET_N = 30


class Risk(str, Enum):
    """How a rider's projected arrival sits against the shift deadline."""

    ARRIVED = "ARRIVED"
    """Already on site. Fact, not projection."""

    ON_TRACK = "ON_TRACK"
    """Projected to make it even at the pessimistic end of the band."""

    AT_RISK = "AT_RISK"
    """Median projection makes it, pessimistic end does not. Advisory only."""

    LATE = "LATE"
    """Median projection misses the deadline."""

    OVERDUE = "OVERDUE"
    """Planned drop has passed with no arrival. Deterministic, no false
    positives. This is what the alert actually fires on."""

    UNKNOWN = "UNKNOWN"
    """No basis to project. Treated as at risk, never as fine."""

    @property
    def needs_attention(self) -> bool:
        return self in {Risk.AT_RISK, Risk.LATE, Risk.OVERDUE, Risk.UNKNOWN}

    @property
    def is_firm(self) -> bool:
        """True when the classification rests on an observation, not a model."""
        return self in {Risk.ARRIVED, Risk.OVERDUE}


@dataclass(frozen=True)
class Projection:
    """A rider's expected arrival, with its uncertainty and provenance."""

    eta: datetime | None
    """Median projected arrival. None means genuinely unknown."""

    spread_min: float
    """Half-width of the p10-p90 band, in minutes. Zero once arrived."""

    risk: Risk
    basis: str
    """Which rung of the ladder produced this. Surfaced in the UI so a manager
    can see whether it is a fact or a model."""

    @property
    def is_known(self) -> bool:
        return self.eta is not None

    @property
    def pessimistic_eta(self) -> datetime | None:
        """The unlucky end of the band."""
        if self.eta is None:
            return None
        return self.eta + timedelta(minutes=self.spread_min)

    def minutes_past(self, deadline: datetime) -> float | None:
        """Minutes late against a deadline. Negative means early."""
        if self.eta is None:
            return None
        return (self.eta - deadline).total_seconds() / 60.0


UNKNOWN = Projection(eta=None, spread_min=0.0, risk=Risk.UNKNOWN, basis="no basis to project")


def _halfhour(ts: datetime) -> int:
    """Half-hour-of-day bucket, matching `v_travel_time.pickup_halfhour`."""
    return ts.hour * 2 + (1 if ts.minute >= 30 else 0)


@lru_cache(maxsize=8)
def _travel_table(office: str) -> dict[tuple[str, int], dict[str, float]]:
    """Load one office's travel-time percentiles. Cached; history never moves."""
    rows = query(
        """
        SELECT trip_nodal, pickup_halfhour, n, n_excess,
               p50_min, p75_min, excess_p50, excess_p75, excess_p90
        FROM v_travel_time
        WHERE office = %s
        """,
        (office,),
    )
    return {
        (r["trip_nodal"], r["pickup_halfhour"]): {
            "n": float(r["n"] or 0),
            "n_excess": float(r["n_excess"] or 0),
            "p50_min": float(r["p50_min"] or DEFAULT_TRAVEL_MIN),
            "p75_min": float(r["p75_min"] or DEFAULT_TRAVEL_MIN),
            "excess_p50": float(r["excess_p50"] or 0.0),
            "excess_p75": float(r["excess_p75"] or DEFAULT_OVERSHOOT_MIN),
            "excess_p90": float(r["excess_p90"] or DEFAULT_SPREAD_MIN),
        }
        for r in rows
    }


def clear_cache() -> None:
    """Drop cached tables. Only needed after a reload, mainly in tests."""
    _travel_table.cache_clear()


def _bucket(office: str, nodal: str | None, when: datetime) -> dict[str, float] | None:
    """Travel-time bucket for a pickup, widening the search when sparse.

    Tries the exact half-hour, then its neighbours, then pools every bucket for
    that pickup type. Odd-hour trips are rare enough that an exact match can
    hold too few observations to mean anything.
    """
    table = _travel_table(office)
    key = nodal or "UNKNOWN"
    target = _halfhour(when)

    for offset in (0, -1, 1, -2, 2):
        bucket = table.get((key, target + offset))
        if bucket and bucket["n_excess"] >= MIN_BUCKET_N:
            return bucket

    same_type = [v for (k, _), v in table.items() if k == key and v["n_excess"] > 0]
    if not same_type:
        return None
    total = sum(v["n_excess"] for v in same_type)
    return {
        field: sum(v[field] * v["n_excess"] for v in same_type) / total
        for field in ("p50_min", "p75_min", "excess_p50", "excess_p75", "excess_p90")
    } | {"n": total, "n_excess": total}


def overshoot_min(office: str, nodal: str | None, when: datetime) -> float:
    """Median minutes a journey runs over its own plan. Unbiased by design."""
    bucket = _bucket(office, nodal, when)
    return bucket["excess_p50"] if bucket else DEFAULT_OVERSHOOT_MIN


def spread_min(office: str, nodal: str | None, when: datetime) -> float:
    """Half-width of the uncertainty band: p90 overshoot minus the median."""
    bucket = _bucket(office, nodal, when)
    if not bucket:
        return DEFAULT_SPREAD_MIN
    return max(1.0, bucket["excess_p90"] - bucket["excess_p50"])


def typical_travel_min(office: str, nodal: str | None, when: datetime) -> float:
    """Typical pickup-to-drop minutes. Only used when a rider has no plan."""
    bucket = _bucket(office, nodal, when)
    return bucket["p50_min"] if bucket else DEFAULT_TRAVEL_MIN


def _classify(
    eta: datetime | None, spread: float, deadline: datetime, overdue: bool
) -> Risk:
    """Turn a projection into a risk band against the shift deadline."""
    if overdue:
        return Risk.OVERDUE
    if eta is None:
        return Risk.UNKNOWN
    if eta > deadline:
        return Risk.LATE
    if eta + timedelta(minutes=spread) > deadline:
        return Risk.AT_RISK
    return Risk.ON_TRACK


def project_arrival(
    leg: Mapping[str, Any], now: datetime, office: str, deadline: datetime | None = None
) -> Projection:
    """Project when a rider reaches the site, using only what `now` reveals.

    The ladder, in descending order of available signal:

    1. Already dropped: a fact.
    2. Planned drop passed with no drop: `OVERDUE`. We stop projecting a time
       and say so. This is the trigger the alert relies on.
    3. Aboard: pickup + own planned travel + median overshoot.
    4. Aboard with no plan: pickup + typical journey for that site and hour.
    5. Not aboard: planned pickup pushed out by the cab's current lateness,
       then travel and overshoot on top.

    Args:
        leg: a row from `v_roster_day`.
        now: the replay clock.
        office: site, selects the history table.
        deadline: shift start plus grace. Only affects the risk band.
    """
    state = rider_state(leg, now)
    if state.is_absent:
        return UNKNOWN

    actual_drop = _observed(leg.get("actual_drop"), now)
    if actual_drop:
        return Projection(
            eta=actual_drop, spread_min=0.0, risk=Risk.ARRIVED, basis="arrived"
        )

    nodal = leg.get("trip_nodal")
    planned_travel = leg.get("planned_travel_min")
    planned_travel = float(planned_travel) if planned_travel is not None else None
    planned_drop = _at(leg.get("planned_drop"))

    # Rung 2. The planned drop has come and gone with nobody stepping out of
    # the cab. Nothing observable says when they will arrive, so we refuse to
    # guess. Deterministic and free of false positives.
    overdue = bool(planned_drop and now > planned_drop)

    actual_pickup = _observed(leg.get("actual_pickup"), now)
    if actual_pickup:
        pad = overshoot_min(office, nodal, actual_pickup)
        spread = spread_min(office, nodal, actual_pickup)
        if planned_travel and planned_travel > 0:
            eta = actual_pickup + timedelta(minutes=planned_travel + pad)
            basis = "pickup + own planned travel + median overshoot"
        else:
            eta = actual_pickup + timedelta(
                minutes=typical_travel_min(office, nodal, actual_pickup)
            )
            basis = "pickup + typical journey for this site and hour"

        if overdue:
            return Projection(
                eta=max(eta, now),
                spread_min=spread,
                risk=Risk.OVERDUE,
                basis="planned drop passed with no arrival",
            )
        # A projection the clock has overtaken is stale but not yet overdue.
        if eta < now:
            eta = now
            basis += " (running behind)"
        risk = _classify(eta, spread, deadline, False) if deadline else Risk.UNKNOWN
        return Projection(eta=eta, spread_min=spread, risk=risk, basis=basis)

    # Not aboard. Anchor on the plan, pushed out by however late the cab is.
    planned_pickup = _at(leg.get("planned_pickup"))
    if planned_pickup is None:
        return UNKNOWN

    expected_pickup = max(planned_pickup + timedelta(minutes=cab_delay_min(leg, now)), now)
    pad = overshoot_min(office, nodal, expected_pickup)
    spread = spread_min(office, nodal, expected_pickup)

    if planned_travel and planned_travel > 0:
        eta = expected_pickup + timedelta(minutes=planned_travel + pad)
        basis = "expected pickup + own planned travel + median overshoot"
    else:
        eta = expected_pickup + timedelta(
            minutes=typical_travel_min(office, nodal, expected_pickup)
        )
        basis = "expected pickup + typical journey for this site and hour"

    if overdue:
        return Projection(
            eta=max(eta, now),
            spread_min=spread,
            risk=Risk.OVERDUE,
            basis="planned drop passed with no pickup recorded",
        )

    # Waiting on a cab that has not come is inherently less certain than being
    # aboard, so widen the band rather than pretend otherwise.
    if state is State.NO_PICKUP:
        spread *= 1.5
    risk = _classify(eta, spread, deadline, False) if deadline else Risk.UNKNOWN
    return Projection(eta=eta, spread_min=spread, risk=risk, basis=basis)


def will_make_shift(
    leg: Mapping[str, Any], now: datetime, office: str, deadline: datetime
) -> bool | None:
    """Will this rider be on the floor by the deadline?

    None means unknown, which callers must treat as at risk rather than as yes.
    """
    projection = project_arrival(leg, now, office, deadline)
    if projection.eta is None:
        return None
    return projection.risk in {Risk.ARRIVED, Risk.ON_TRACK, Risk.AT_RISK}
