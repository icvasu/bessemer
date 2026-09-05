"""Rider state machine.

At any replay instant the agent must answer one question per rostered rider:
where are they, and will they make the shift? This module answers the first
half. Everything here is a pure function of one leg row plus `now`, so it is
trivially testable and cannot drift from the data.

The cardinal rule: **only look at what would have been observable at `now`.**
The database row carries the whole day including the eventual drop time, so
every accessor here is guarded by a timestamp comparison. Reading
`actual_drop` without checking it against `now` would leak the future into the
projection and make the demo a lie.

Absence has two shapes and the line manager treats them differently:

  CANCELLED  someone cancelled in advance (leave, WFH). Known. Adjust the
             roster, no alert.
  NO_SHOW    the rider never boarded and nobody told anyone. Unknown. Alert.

Between them sits NO_PICKUP: the planned pickup has passed with no pickup
recorded, but the day is not over. Suspicion, not fact. It is what lets the
agent warn the manager before the cab company has admitted anything.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Mapping

from app.config import NO_PICKUP_GRACE_MIN

# Cancellation reasons the transport system records in advance. Anything else
# in `not_boarding_reason` is an unexplained absence.
CANCELLED_REASONS = frozenset({"TRIP_CANCELLED_FROM_DASHBOARD"})


class State(str, Enum):
    """Where a rider is, as far as the agent can tell at `now`."""

    SCHEDULED = "SCHEDULED"
    """Rostered today, but their cab has not been due to start yet."""

    CAB_MOVING = "CAB_MOVING"
    """The cab has started its route. The rider is not aboard yet."""

    CAB_LATE = "CAB_LATE"
    """The cab's planned start has passed with no start recorded.
    The earliest warning the data can give."""

    PICKED_UP = "PICKED_UP"
    """Aboard and en route. This is where the ETA projection matters."""

    DROPPED = "DROPPED"
    """On site. The only state that counts towards floor strength."""

    NO_PICKUP = "NO_PICKUP"
    """Planned pickup plus grace has passed with no pickup. Suspected absence,
    not yet confirmed. Actionable: someone should call them."""

    NO_SHOW = "NO_SHOW"
    """Confirmed not travelling, with no advance notice."""

    CANCELLED = "CANCELLED"
    """Confirmed not travelling, cancelled in advance. Expected absence."""

    NOT_SCHEDULED = "NOT_SCHEDULED"
    """On the roster but with no leg today. Rest day or unrostered."""

    @property
    def is_present(self) -> bool:
        """Counts towards floor strength right now."""
        return self is State.DROPPED

    @property
    def is_travelling(self) -> bool:
        """Expected to arrive; the ETA is meaningful."""
        return self in {State.SCHEDULED, State.CAB_MOVING, State.CAB_LATE, State.PICKED_UP}

    @property
    def is_absent(self) -> bool:
        """Will not arrive for this shift."""
        return self in {State.NO_SHOW, State.CANCELLED, State.NOT_SCHEDULED}

    @property
    def is_uncertain(self) -> bool:
        """Neither travelling nor confirmed absent. Needs a human."""
        return self is State.NO_PICKUP


def _at(value: Any) -> datetime | None:
    """Coerce a cell to a datetime, treating pandas/psycopg nulls as None."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    # pandas NaT and numpy NaN both fail this identity check.
    if value != value:  # noqa: PLR0124 - NaN is the only value unequal to itself
        return None
    return value


def _observed(value: Any, now: datetime) -> datetime | None:
    """Return a timestamp only if it has already happened at `now`.

    This is the guard that keeps the replay honest. Every future-bearing column
    goes through it.
    """
    ts = _at(value)
    if ts is None or ts > now:
        return None
    return ts


def cab_delay_min(leg: Mapping[str, Any], now: datetime) -> float:
    """Minutes the cab is behind its planned start, as observable at `now`.

    Before the cab starts this grows with the clock, which is what lets the
    agent flag a rider as at risk before anyone has been picked up.
    """
    planned_start = _at(leg.get("planned_start"))
    if planned_start is None:
        return 0.0

    actual_start = _observed(leg.get("actual_start"), now)
    reference = actual_start or now
    return max(0.0, (reference - planned_start).total_seconds() / 60.0)


def rider_state(leg: Mapping[str, Any] | None, now: datetime) -> State:
    """Classify one rider at one instant.

    Args:
        leg: a row from `v_roster_day`, or None if the rider has no leg today.
        now: the replay clock.

    Returns:
        The rider's observable state.
    """
    if leg is None:
        return State.NOT_SCHEDULED

    # --- terminal absences ------------------------------------------------
    #
    # These are known at roster time, not discovered during the commute, so
    # they are not gated on `now`. A dashboard cancellation is entered before
    # the trip; a no-show flag is only meaningful once the trip has run, and
    # the pickup guard below reaches the same conclusion earlier anyway.
    reason = leg.get("not_boarding_reason")
    if reason in CANCELLED_REASONS:
        return State.CANCELLED

    if _observed(leg.get("actual_drop"), now):
        return State.DROPPED

    if _observed(leg.get("actual_pickup"), now):
        return State.PICKED_UP

    # --- not aboard yet ---------------------------------------------------
    #
    # The planned pickup has passed with nothing recorded. Two very different
    # situations look identical if you only read the rider's row:
    #
    #   the cab is late   -> the rider is standing at their stop, blameless
    #   the rider is not  -> the cab came and went, or will find nobody there
    #
    # Telling them apart matters, because the first calls for patience and the
    # second calls for a phone call. The cab's own start time separates them:
    # a cab that left 20 minutes late cannot have collected anyone on time, so
    # we push the expected pickup out by that lateness before judging anybody.
    #
    # Without this, riders showed NO_PICKUP for half an hour purely because
    # their cab was running behind, and every one of those suspicions was
    # wrong.
    planned_pickup = _at(leg.get("planned_pickup"))
    if planned_pickup:
        expected_pickup = planned_pickup + timedelta(minutes=cab_delay_min(leg, now))
        overdue_at = expected_pickup + timedelta(minutes=NO_PICKUP_GRACE_MIN)
        if now >= overdue_at:
            if leg.get("boarding_status") == "Not Boarded":
                return State.NO_SHOW
            return State.NO_PICKUP

    # --- still upstream of the pickup ------------------------------------
    if _observed(leg.get("actual_start"), now):
        return State.CAB_MOVING

    planned_start = _at(leg.get("planned_start"))
    if planned_start and now >= planned_start:
        return State.CAB_LATE

    return State.SCHEDULED


def pickup_delay_min(leg: Mapping[str, Any], now: datetime) -> float:
    """Minutes the rider's pickup is behind plan, as observable at `now`."""
    planned_pickup = _at(leg.get("planned_pickup"))
    if planned_pickup is None:
        return 0.0

    actual_pickup = _observed(leg.get("actual_pickup"), now)
    reference = actual_pickup or now
    return max(0.0, (reference - planned_pickup).total_seconds() / 60.0)
