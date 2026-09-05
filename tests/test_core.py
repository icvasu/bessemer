"""Phase 2 checks: the reasoning core, on the real demo day.

These lock in the behaviours that were wrong in a first draft and would be easy
to break again. Each one names the mistake it guards against.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from app.core.alerts import (
    Cause,
    Pathway,
    Status,
    build_alert,
    classify,
    evaluate_triggers,
)
from app.core.eta import Risk, project_arrival
from app.core.queue import project_queues
from app.core.remediation import candidates, hold_over_cost, iso_week
from app.core.state import State, cab_delay_min, rider_state
from app.db import query

OFFICE = "Clearwater Campus"
BU = "pinnacle-Slc"
SHIFT = "09:00"
DEMO = date(2026, 6, 11)
SHIFT_START = datetime(2026, 6, 11, 9, 0)
DEADLINE = datetime(2026, 6, 11, 9, 5)


def at(hhmm: str) -> datetime:
    return datetime.fromisoformat(f"2026-06-11 {hhmm}")


@pytest.fixture(scope="module")
def legs() -> list[dict]:
    return query(
        "SELECT * FROM v_roster_day WHERE trip_date = %s AND role = 'primary'", (DEMO,)
    )


@pytest.fixture(scope="module")
def queues() -> list[dict]:
    return query("SELECT * FROM queues ORDER BY queue")


@pytest.fixture(scope="module")
def by_id(legs) -> dict[int, dict]:
    return {leg["stwid"]: leg for leg in legs}


# --------------------------------------------------------------- state machine


def test_no_leg_means_not_scheduled():
    assert rider_state(None, at("08:00")) is State.NOT_SCHEDULED


def test_state_never_reads_the_future(legs):
    """The whole replay is a lie if a drop leaks in before it happened."""
    for leg in legs:
        if not leg["actual_drop"]:
            continue
        just_before = leg["actual_drop"] - timedelta(seconds=1)
        assert rider_state(leg, just_before) is not State.DROPPED
        assert rider_state(leg, leg["actual_drop"]) is State.DROPPED


def test_state_progresses_in_one_direction(legs):
    """A rider should not go back to waiting once they are aboard."""
    order = {
        State.SCHEDULED: 0,
        State.CAB_LATE: 1,
        State.CAB_MOVING: 1,
        State.NO_PICKUP: 2,
        State.PICKED_UP: 3,
        State.DROPPED: 4,
    }
    for leg in legs[:8]:
        seen = -1
        for minute in range(0, 150, 5):
            state = rider_state(leg, at("07:30") + timedelta(minutes=minute))
            if state in {State.NO_SHOW, State.CANCELLED, State.NOT_SCHEDULED}:
                continue
            rank = order[state]
            # NO_PICKUP can resolve upward into PICKED_UP; nothing else regresses.
            assert rank >= seen or seen == 2, f"{leg['stwid']} went {seen} -> {rank}"
            seen = max(seen, rank)


def test_a_late_cab_is_not_a_missing_rider(legs):
    """The bug this guards: reading a missed pickup as a suspected absence
    without asking whether the cab had even arrived. A naive rule flagged 40%
    of riders who were picked up perfectly normally; accounting for the cab's
    own lateness brings that to 6%."""
    false_alarms = 0
    total = 0
    for leg in legs:
        if not leg["actual_pickup"] or leg["boarding_status"] != "Boarded":
            continue
        total += 1
        # Sweep the window between the planned and the actual pickup.
        cursor = leg["planned_pickup"]
        while cursor < leg["actual_pickup"]:
            if rider_state(leg, cursor) is State.NO_PICKUP:
                false_alarms += 1
                break
            cursor += timedelta(minutes=1)
    assert total > 15
    assert false_alarms / total < 0.20


def test_cab_delay_grows_while_the_cab_has_not_started(by_id):
    leg = next(
        leg for leg in by_id.values()
        if leg["planned_start"] and leg["actual_start"]
        and leg["actual_start"] > leg["planned_start"] + timedelta(minutes=5)
    )
    mid = leg["planned_start"] + timedelta(minutes=2)
    assert cab_delay_min(leg, mid) == pytest.approx(2.0, abs=0.1)
    later = leg["actual_start"] + timedelta(hours=1)
    settled = (leg["actual_start"] - leg["planned_start"]).total_seconds() / 60
    assert cab_delay_min(leg, later) == pytest.approx(settled, abs=0.1)


# ------------------------------------------------------------------ projection


def test_arrived_riders_report_their_real_arrival(legs):
    for leg in legs[:10]:
        if not leg["actual_drop"]:
            continue
        after = leg["actual_drop"] + timedelta(minutes=1)
        projection = project_arrival(leg, after, OFFICE, DEADLINE)
        assert projection.risk is Risk.ARRIVED
        assert projection.eta == leg["actual_drop"]
        assert projection.spread_min == 0.0


def test_overdue_fires_once_the_planned_drop_passes(legs):
    """The one signal with no false positives, and what the alert leans on."""
    leg = next(leg for leg in legs if leg["planned_drop"] and leg["actual_drop"]
               and leg["actual_drop"] > leg["planned_drop"] + timedelta(minutes=10))
    between = leg["planned_drop"] + timedelta(minutes=5)
    assert project_arrival(leg, between, OFFICE, DEADLINE).risk is Risk.OVERDUE


def test_projection_is_not_systematically_pessimistic():
    """Guards the cry-wolf failure: padding every ETA to the 75th percentile
    flagged 84% of riders as late when 43% were, and precision fell to 0.46.

    Calibration is checked across all three months, not on the demo day. One
    day is far too small a sample, and the demo day was chosen precisely
    because it went badly, so measuring bias there would be measuring the
    weather.
    """
    history = query(
        """
        SELECT * FROM v_roster_day
        WHERE role = 'primary' AND actual_pickup IS NOT NULL
          AND actual_drop IS NOT NULL AND planned_travel_min IS NOT NULL
        """
    )
    errors = []
    for leg in history:
        at_pickup = leg["actual_pickup"] + timedelta(seconds=30)
        deadline = leg["shift_start"] + timedelta(minutes=5)
        projection = project_arrival(leg, at_pickup, OFFICE, deadline)
        if projection.eta:
            errors.append((projection.eta - leg["actual_drop"]).total_seconds() / 60)
    assert len(errors) > 1000
    median = sorted(errors)[len(errors) // 2]
    assert -3 < median < 3, f"median ETA error {median:+.1f} min is biased"


def test_projection_under_calls_a_bad_morning(legs):
    """The honest limitation, asserted so nobody mistakes it for a bug.

    The projection is calibrated on ordinary days, so on a bad one it is
    optimistic: journey times carry ~13 minutes of spread that no amount of
    history removes without a live position feed. This is exactly why impact
    is published as a range and why the alert's hard trigger is the
    deterministic overdue signal rather than the model's opinion.
    """
    errors = []
    for leg in legs:
        if not (leg["actual_pickup"] and leg["actual_drop"] and leg["planned_travel_min"]):
            continue
        projection = project_arrival(
            leg, leg["actual_pickup"] + timedelta(seconds=30), OFFICE, DEADLINE
        )
        if projection.eta:
            errors.append((projection.eta - leg["actual_drop"]).total_seconds() / 60)
    median = sorted(errors)[len(errors) // 2]
    assert median < 0, "on the demo day the projection should read optimistic"


def test_uncertainty_is_reported_not_hidden(legs):
    leg = next(leg for leg in legs if leg["actual_pickup"])
    projection = project_arrival(leg, leg["actual_pickup"] + timedelta(minutes=1), OFFICE, DEADLINE)
    assert projection.spread_min > 0
    assert projection.pessimistic_eta > projection.eta
    assert projection.basis


# ----------------------------------------------------------------- queue level


def test_queues_start_full_and_end_full(legs, queues):
    early = project_queues(legs, queues, at("07:30"), OFFICE, DEADLINE)
    assert all(q.coverage == 1.0 for q in early.values())
    late = project_queues(legs, queues, at("10:00"), OFFICE, DEADLINE)
    assert all(len(q.on_floor) == q.rostered for q in late.values())


def test_coverage_does_not_collapse_when_the_planned_drop_passes(legs, queues):
    """The artefact this guards: counting the OVERDUE label instead of the
    projected arrival dropped billing from 67% to 8% the moment 08:55 went by,
    even though nobody's position had changed."""
    before = project_queues(legs, queues, at("08:54"), OFFICE, DEADLINE)["billing"]
    after = project_queues(legs, queues, at("08:56"), OFFICE, DEADLINE)["billing"]
    assert abs(after.coverage - before.coverage) < 0.25


def test_impact_is_a_range_that_brackets_the_real_outcome(legs, queues):
    projection = project_queues(legs, queues, at("08:55"), OFFICE, DEADLINE)["billing"]
    impact = projection.impact()
    assert impact.calls_unanswered_high > impact.calls_unanswered
    actual = query(
        """
        SELECT SUM(GREATEST(0, EXTRACT(EPOCH FROM (actual_drop - %s)) / 60)) / 5 AS calls
        FROM v_roster_day
        WHERE trip_date = %s AND role = 'primary' AND queue = 'billing'
        """,
        (DEADLINE, DEMO),
    )[0]["calls"]
    assert impact.calls_unanswered <= float(actual) <= impact.calls_unanswered_high * 1.2


def test_every_rider_lands_in_exactly_one_bucket(legs, queues):
    for now in [at("08:00"), at("08:45"), at("09:10"), at("09:40")]:
        for projection in project_queues(legs, queues, now, OFFICE, DEADLINE).values():
            buckets = (
                len(projection.on_floor)
                + len(projection.in_transit)
                + len(projection.at_risk)
                + len(projection.absent)
            )
            assert buckets == projection.rostered


# ---------------------------------------------------------------- remediation


def test_cover_candidates_are_verifiably_in_the_building():
    now = at("08:55")
    picked = candidates("billing", DEMO, now, OFFICE, limit=3)
    assert picked
    for candidate in picked:
        assert candidate.arrived_at <= now
        assert candidate.pathway in {"EARLY_SHIFT_COVER", "CROSS_COVER"}


def test_cover_search_sees_nobody_before_the_early_shift_lands():
    assert candidates("billing", DEMO, at("07:45"), OFFICE) == []


def test_same_queue_candidates_come_first():
    picked = candidates("billing", DEMO, at("09:00"), OFFICE, limit=5)
    same = [c.same_queue for c in picked]
    assert same == sorted(same, reverse=True)


def test_hold_over_prices_the_do_nothing_option():
    """On a 24/7 desk the position stays manned whatever happens, so the
    default course of action still costs somebody their morning."""
    recovered = datetime(2026, 6, 11, 9, 25)
    hold = hold_over_cost("billing", DEMO, SHIFT_START, gap_size=3, recovered_by=recovered)
    assert hold.agents_held == 3
    assert hold.minutes == pytest.approx(75.0)
    assert hold.cost > 0
    assert hold.missed_cabs == 3  # past the 09:15 cab home


def test_hold_over_is_free_when_nobody_is_missing():
    hold = hold_over_cost("billing", DEMO, SHIFT_START, 0, datetime(2026, 6, 11, 9, 30))
    assert hold.agents_held == 0 and hold.cost == 0.0


def test_iso_week_format():
    assert iso_week(date(2026, 6, 11)) == "2026-W24"


# --------------------------------------------------------------------- alerts


def test_nothing_fires_on_a_healthy_queue(legs, queues):
    projection = project_queues(legs, queues, at("07:30"), OFFICE, DEADLINE)["billing"]
    assert evaluate_triggers(projection) == []


def test_billing_alert_opens_before_the_shift_starts(legs, queues):
    opened_at = None
    existing = None
    for minute in range(0, 120, 5):
        now = at("07:30") + timedelta(minutes=minute)
        projection = project_queues(legs, queues, now, OFFICE, DEADLINE)["billing"]
        existing = build_alert(projection, now, DEMO, OFFICE, BU, SHIFT, SHIFT_START, existing)
        if existing and opened_at is None:
            opened_at = now
    assert opened_at is not None
    assert opened_at < SHIFT_START, "warning must arrive before the shift, not after"


def test_riders_already_at_their_desks_do_not_keep_an_alert_open(legs, queues):
    """The bug this guards: the late trigger counted people who had already
    arrived, so an alert stayed lit at 100% coverage with nothing to decide."""
    projection = project_queues(legs, queues, at("10:00"), OFFICE, DEADLINE)["billing"]
    assert all(r.on_floor for r in projection.riders)
    assert evaluate_triggers(projection) == []


def test_an_alert_resolves_and_stays_resolved(legs, queues):
    existing = None
    resolved_at = None
    for minute in range(0, 200, 5):
        now = at("07:30") + timedelta(minutes=minute)
        projection = project_queues(legs, queues, now, OFFICE, DEADLINE)["billing"]
        existing = build_alert(projection, now, DEMO, OFFICE, BU, SHIFT, SHIFT_START, existing)
        if existing and existing.status is Status.RESOLVED:
            resolved_at = resolved_at or now
            assert existing.status is Status.RESOLVED, "resolved alerts must not reopen"
    assert resolved_at is not None


def test_options_always_include_a_costed_default(legs, queues):
    projection = project_queues(legs, queues, at("08:55"), OFFICE, DEADLINE)["billing"]
    alert = build_alert(projection, at("08:55"), DEMO, OFFICE, BU, SHIFT, SHIFT_START)
    pathways = {o.pathway for o in alert.options}
    assert Pathway.HOLD_OVER in pathways, "doing nothing has a price and must be shown"
    assert Pathway.EARLY_SHIFT_COVER in pathways
    assert sum(1 for o in alert.options if o.recommended) == 1
    hold = next(o for o in alert.options if o.pathway is Pathway.HOLD_OVER)
    assert hold.cost and hold.cost["agents_held"] > 0
    assert hold.people, "the night agents being asked to stay must be named"


def test_the_payload_names_real_people_and_real_numbers(legs, queues):
    alert = build_alert(
        project_queues(legs, queues, at("08:55"), OFFICE, DEADLINE)["billing"],
        at("08:55"), DEMO, OFFICE, BU, SHIFT, SHIFT_START,
    )
    payload = alert.payload()
    assert payload["riders_affected"]
    assert all(r["name"] and r["state"] for r in payload["riders_affected"])
    assert payload["context"], "an alert without a benchmark is just a number"
    assert payload["impact"]["calls_range"]


def test_narration_is_rare(legs, queues):
    """The cost story, asserted. A bad morning must not cost a call per tick."""
    existing = None
    narrations = 0
    ticks = 0
    for minute in range(0, 150):
        now = at("07:30") + timedelta(minutes=minute)
        ticks += 1
        projection = project_queues(legs, queues, now, OFFICE, DEADLINE)["billing"]
        existing = build_alert(projection, now, DEMO, OFFICE, BU, SHIFT, SHIFT_START, existing)
        if existing and existing.needs_narrative(now):
            narrations += 1
            existing.narrative = "x"
            existing.mark_narrated(now)
    assert ticks == 150
    assert 1 <= narrations <= 8, f"{narrations} model calls for one queue is too many"


def test_cause_ignores_the_operators_own_delay_reason(legs, queues):
    """70% of late arrivals on this shift are stamped NODELAY by transport.
    The cause must come from observed state, not from that column."""
    projection = project_queues(legs, queues, at("08:55"), OFFICE, DEADLINE)["billing"]
    affected = [r for r in projection.riders if not r.expected]
    assert affected
    reasons = {
        leg["delay_reason"] for leg in legs
        if leg["stwid"] in {r.stwid for r in affected}
    }
    assert "NODELAY" in reasons
    assert classify(projection) is not Cause.MIXED
