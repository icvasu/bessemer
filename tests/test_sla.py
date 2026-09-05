"""Service level: the metric the line manager reports upward.

These check the queueing maths against known properties rather than against
numbers I typed in, so they stay meaningful if the forecast is retuned.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from app.core.alerts import Pathway, build_alert
from app.core.queue import project_queues
from app.core.sla import (
    adherence,
    agents_needed,
    erlang_b,
    erlang_c,
    parse_target,
    project_day,
    service_level,
)
from app.db import query

OFFICE = "Clearwater Campus"
DEMO = date(2026, 6, 11)
DEADLINE = datetime(2026, 6, 11, 9, 5)


@pytest.fixture(scope="module")
def legs():
    return query(
        "SELECT * FROM v_roster_day WHERE trip_date = %s AND role = 'primary'", (DEMO,)
    )


@pytest.fixture(scope="module")
def queues():
    return query("SELECT * FROM queues ORDER BY queue")


# ------------------------------------------------------------------- the maths


def test_target_parsing_and_fallback():
    assert parse_target("80/20") == (80.0, 20.0)
    assert parse_target("90/15") == (90.0, 15.0)
    assert parse_target(None) == (80.0, 20.0)
    assert parse_target("nonsense") == (80.0, 20.0)


def test_erlang_b_is_a_probability_and_falls_with_agents():
    values = [erlang_b(n, 10.0) for n in range(1, 30)]
    assert all(0.0 <= v <= 1.0 for v in values)
    assert values == sorted(values, reverse=True)


def test_erlang_b_survives_large_agent_counts():
    """The textbook factorial form overflows here. The recursion must not."""
    assert 0.0 <= erlang_b(150, 120.0) <= 1.0


def test_erlang_c_saturates_when_load_exceeds_capacity():
    assert erlang_c(5, 6.0) == 1.0
    assert erlang_c(20, 5.0) < 0.05


def test_service_level_rises_with_headcount():
    levels = [service_level(n, 46, 5.0, 30, "80/20").service_level for n in range(7, 20)]
    assert levels == sorted(levels)
    assert levels[0] < 0.5 < levels[-1]


def test_service_level_collapses_non_linearly():
    """The whole reason this module exists. Removing a third of a loaded queue
    must not cost a third of the service level, it must cost far more."""
    full = service_level(12, 46, 5.0, 30, "80/20").service_level
    short = service_level(8, 46, 5.0, 30, "80/20").service_level
    assert full > 0.85
    assert short < full / 3, "a headcount metric would have missed this"


def test_an_unmanned_queue_answers_nothing():
    result = service_level(0, 46, 5.0, 30, "80/20")
    assert result.service_level == 0.0
    assert result.abandon_rate == 1.0
    assert "unmanned" in result.headline


def test_overloaded_queue_reports_a_growing_backlog():
    result = service_level(6, 46, 5.0, 30, "80/20")
    assert result.is_overloaded
    assert result.service_level == 0.0
    assert result.backlog_calls > 0
    assert "backing up" in result.headline


def test_agents_needed_is_the_smallest_headcount_that_holds():
    needed = agents_needed(46, 5.0, 30, "80/20")
    assert service_level(needed, 46, 5.0, 30, "80/20").meets_target
    assert not service_level(needed - 1, 46, 5.0, 30, "80/20").meets_target


def test_the_roster_is_staffed_with_about_one_agent_of_headroom(queues):
    """Guards the calibration. A lighter forecast leaves the queue at 50%
    occupancy where staffing barely moves the service level; a heavier one
    breaches at full strength every morning regardless of any commute."""
    for row in queues:
        needed = agents_needed(
            row["calls_per_30min"], float(row["aht_min"]), 30, row["sl_target"]
        )
        assert needed == 11, f"{row['queue']} needs {needed} of 12; retune the forecast"


# ---------------------------------------------------------------- the day view


def test_a_short_disruption_does_not_break_the_day():
    """Just as important as catching a breach: telling the manager when not
    to escalate."""
    day = project_day([(8, 1.0)], 12, 46, 5.0, 8.0, 30.0, "80/20")
    assert day.meets_target
    assert day.intervals_breached == 1


def test_a_sustained_shortfall_does_break_the_day():
    day = project_day([(8, 1.0)] * 4, 12, 46, 5.0, 8.0, 30.0, "80/20")
    assert not day.meets_target
    assert not day.recoverable


def test_partial_intervals_are_weighted_not_rounded_up():
    """A gap closing ten minutes in should not cost the whole half hour."""
    whole = project_day([(8, 1.0)], 12, 46, 5.0, 8.0, 30.0, "80/20")
    third = project_day([(8, 0.33)], 12, 46, 5.0, 8.0, 30.0, "80/20")
    assert third.day_service_level > whole.day_service_level


def test_day_projection_degrades_monotonically():
    levels = [
        project_day([(8, 1.0)] * n, 12, 46, 5.0, 8.0, 30.0, "80/20").day_service_level
        for n in range(0, 9)
    ]
    assert levels == sorted(levels, reverse=True)


def test_adherence_counts_lost_minutes_against_scheduled_time():
    result = adherence(rostered=12, minutes_lost=240, worst_case_minutes_lost=400)
    assert result.minutes_scheduled == 12 * 8 * 60
    assert result.adherence == pytest.approx(1 - 240 / 5760)
    assert "agent-minutes lost" in result.headline


# ------------------------------------------------------- wired into the alert


def test_impact_carries_the_contracted_metrics(legs, queues):
    projection = project_queues(legs, queues, datetime(2026, 6, 11, 8, 55), OFFICE, DEADLINE)
    impact = projection["billing"].impact()
    assert impact.service_level is not None
    assert impact.service_level_full is not None
    assert impact.day is not None
    assert impact.adherence is not None
    assert impact.agents_needed > 0
    # Full strength must look healthy, the shortfall must not.
    assert impact.service_level_full.meets_target
    assert not impact.service_level.meets_target


def test_the_demo_day_collapses_the_interval_but_not_the_day(legs, queues):
    """The honest headline, asserted. A twenty-minute gap is a floor problem,
    not a contract problem, and the alert has to be able to say so."""
    impact = project_queues(
        legs, queues, datetime(2026, 6, 11, 8, 55), OFFICE, DEADLINE
    )["billing"].impact()
    assert impact.service_level.service_level < 0.3
    assert impact.day.meets_target


def test_every_staffing_option_carries_its_service_level(legs, queues):
    now = datetime(2026, 6, 11, 8, 55)
    alert = build_alert(
        project_queues(legs, queues, now, OFFICE, DEADLINE)["billing"],
        now, DEMO, OFFICE, "pinnacle-Slc", "09:00", datetime(2026, 6, 11, 9, 0),
    )
    staffing = [
        o for o in alert.options
        if o.pathway in {Pathway.HOLD_OVER, Pathway.EARLY_SHIFT_COVER, Pathway.CROSS_COVER}
    ]
    assert staffing
    for option in staffing:
        assert option.service_level is not None
        assert option.agents_after is not None
        assert option.outcome


def test_filling_the_gap_beats_leaving_it(legs, queues):
    now = datetime(2026, 6, 11, 8, 55)
    projection = project_queues(legs, queues, now, OFFICE, DEADLINE)["billing"]
    alert = build_alert(
        projection, now, DEMO, OFFICE, "pinnacle-Slc", "09:00", datetime(2026, 6, 11, 9, 0)
    )
    cover = next(o for o in alert.options if o.pathway is Pathway.EARLY_SHIFT_COVER)
    assert cover.service_level.service_level > projection.impact().service_level.service_level


def test_operations_is_not_escalated_to_over_a_half_hour_dip(legs, queues):
    """Escalating a recoverable blip trains a director to ignore the channel."""
    now = datetime(2026, 6, 11, 8, 55)
    alert = build_alert(
        project_queues(legs, queues, now, OFFICE, DEADLINE)["billing"],
        now, DEMO, OFFICE, "pinnacle-Slc", "09:00", datetime(2026, 6, 11, 9, 0),
    )
    assert Pathway.ESCALATE_OPS not in {o.pathway for o in alert.options}


def test_operations_is_escalated_to_when_the_day_is_lost(legs, queues):
    """Same machinery, sustained problem: half the queue never turns up.

    The clock has to be advanced for this rather than the damage simply being
    asserted, because the projection genuinely cannot foresee a shortfall that
    has not happened yet. Without a live position feed it learns the same way a
    manager does, by watching people fail to arrive. Escalation therefore comes
    later than the first alert, and that ordering is the point of the test: the
    half-hour dip is handled on the floor, and only a problem that persists
    reaches the director.
    """
    damaged = [dict(leg) for leg in legs if leg["queue"] == "billing"]
    for leg in damaged[:7]:
        # Never picked up and never dropped: the cab came and went.
        leg["actual_pickup"] = None
        leg["actual_drop"] = None
        leg["boarding_status"] = "Not Boarded"
        leg["not_boarding_reason"] = "NO_SHOW"

    billing = [q for q in queues if q["queue"] == "billing"]
    shift_start = datetime(2026, 6, 11, 9, 0)
    escalated_at = None
    existing = None

    for minute in range(0, 180, 5):
        now = datetime(2026, 6, 11, 9, 0) + timedelta(minutes=minute)
        alert = build_alert(
            project_queues(damaged, billing, now, OFFICE, DEADLINE)["billing"],
            now, DEMO, OFFICE, "pinnacle-Slc", "09:00", shift_start, existing,
        )
        if alert is None:
            continue
        existing = alert
        if Pathway.ESCALATE_OPS in {o.pathway for o in alert.options}:
            escalated_at = escalated_at or now

    assert escalated_at is not None, "a lost day must reach operations"
    assert escalated_at >= shift_start, "escalation should follow evidence, not precede it"


def test_a_permanent_shortfall_does_not_look_like_a_recovered_one(legs, queues):
    """The bug this guards, which was the worst one in this module.

    Confirmed absences carry no arrival time. Treating "no ETA" as "nothing to
    project" meant a queue seven agents down for the whole shift reported a
    healthy day, because the one condition that never recovers was indexed the
    same way as the one that already had.
    """
    damaged = [dict(leg) for leg in legs if leg["queue"] == "billing"]
    for leg in damaged[:7]:
        leg["actual_pickup"] = None
        leg["actual_drop"] = None
        leg["boarding_status"] = "Not Boarded"
        leg["not_boarding_reason"] = "NO_SHOW"

    billing = [q for q in queues if q["queue"] == "billing"]
    impact = project_queues(
        damaged, billing, datetime(2026, 6, 11, 9, 30), OFFICE, DEADLINE
    )["billing"].impact()

    assert impact.day.intervals_breached == impact.day.intervals_total
    assert not impact.day.meets_target
    assert not impact.day.recoverable
