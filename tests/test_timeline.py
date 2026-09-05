"""The story slider: a captured morning that a presenter can scrub through.

Built without narration here, so the suite stays free. The narrated path is
the same loop with one awaited call inside it, and the live agent tests cover
that call on its own.
"""

from __future__ import annotations

import asyncio
from datetime import date

import pytest
from fastapi.testclient import TestClient

from app import store
from app import timeline as tl
from app.api import app
from app.sessions import SESSIONS, get_session

DEMO = date(2026, 6, 11)
OFFICE = "Clearwater Campus"


@pytest.fixture()
def client():
    SESSIONS.clear()
    with TestClient(app) as c:
        c.post("/replay/reset", params={"clear_cover": True})
        yield c
        c.post("/replay/reset", params={"clear_cover": True})
    SESSIONS.clear()


@pytest.fixture(scope="module")
def built():
    """One captured morning, shared across the module. About two seconds."""
    SESSIONS.clear()
    store.reset_shift(OFFICE, DEMO, "09:00")
    session = get_session("pinnacle-Slc", OFFICE, DEMO, "09:00")
    timeline = asyncio.run(tl.build(session, narrate=False))
    yield session, timeline
    store.reset_shift(OFFICE, DEMO, "09:00")
    SESSIONS.clear()


# ------------------------------------------------------------------ capture


def test_every_minute_of_the_morning_is_captured(built):
    _, timeline = built
    assert timeline.ready and not timeline.error
    assert timeline.ticks_done == timeline.ticks_total == 151
    assert timeline.keys[0] == "07:30"
    assert timeline.keys[-1] == "10:00"


def test_snapshots_are_independent_of_each_other(built):
    """The alert objects mutate in place as the clock runs. If snapshots held
    references rather than copies, every minute would show the final state."""
    _, timeline = built
    early = timeline.at("08:30")["board"]["totals"]
    late = timeline.at("09:30")["board"]["totals"]
    assert early["on_floor"] < late["on_floor"]
    assert early["clock"] if "clock" in early else True


def test_a_snapshot_is_what_the_live_clock_would_have_shown(built):
    """The whole point: precomputed is not a different computation."""
    from app.replay import Replay

    _, timeline = built
    live = Replay(shift_date=DEMO, tick_minutes=1)
    for tick in live.ticks():
        live.advance(tick)
        if tick.strftime("%H:%M") == "08:55":
            break
    snap = timeline.at("08:55")["board"]
    assert snap["totals"] == live.board()["totals"]
    for a, b in zip(snap["queues"], live.board()["queues"]):
        assert a["on_floor"] == b["on_floor"]
        assert a["impact"]["service_level"]["service_level_pct"] == b["impact"]["service_level"]["service_level_pct"]


def test_asking_for_a_minute_between_ticks_gives_the_one_before(built):
    _, timeline = built
    assert timeline.at("08:55")["time"] == "08:55"
    assert timeline.at("07:00")["time"] == "07:30"  # before the start: clamp
    assert timeline.at("11:00")["time"] == "10:00"  # after the end: clamp


def test_the_buckets_add_up_at_every_minute(built):
    """Guards the bug this feature exposed: "on the way" read zero all morning
    because it was bucketed on the pessimistic band while the percentage beside
    it used the median."""
    _, timeline = built
    seen_transit = False
    for key in timeline.keys:
        for q in timeline.snapshots[key]["board"]["queues"]:
            total = q["on_floor"] + q["in_transit"] + q["at_risk"] + q["absent"]
            assert total == q["rostered"], f"{key} {q['queue']}: {total} != {q['rostered']}"
            if q["in_transit"] > 0:
                seen_transit = True
    assert seen_transit, "'on the way' must be non-zero at some point in the morning"


# ---------------------------------------------------------------- landmarks


def test_landmarks_are_read_off_the_feed_not_hand_placed(built):
    _, timeline = built
    labels = [m.label for m in timeline.landmarks]
    assert any("alert opens" in l for l in labels)
    assert any("not collected" in l for l in labels)
    assert any("back to strength" in l for l in labels)
    assert "Shift starts" in labels
    assert "Grace period ends" in labels


def test_landmarks_are_ordered_and_one_per_minute(built):
    _, timeline = built
    times = [m.at for m in timeline.landmarks]
    assert times == sorted(times)
    assert len(times) == len(set(times))


def test_there_are_few_enough_landmarks_to_present(built):
    """Every arrival is in the feed. None of them is a landmark."""
    _, timeline = built
    assert 5 <= len(timeline.landmarks) <= 14


# --------------------------------------------------------------------- api


def test_the_slider_endpoints_refuse_until_built(client):
    assert client.get("/timeline", params={"at": "08:55"}).status_code == 409
    status = client.get("/timeline/status").json()
    assert status["ready"] is False and status["building"] is False


def test_build_runs_in_the_background_and_reports_progress(client):
    import time

    started = client.post("/timeline/build", params={"narrate": False}).json()
    assert started["status"] in {"building", "already building"}
    for _ in range(100):
        status = client.get("/timeline/status").json()
        if status["ready"]:
            break
        time.sleep(0.1)
    assert status["ready"]
    assert status["ticks_done"] == status["ticks_total"]
    assert status["landmarks"]


def test_a_snapshot_carries_board_alerts_feed_and_landmarks(client):
    import time

    client.post("/timeline/build", params={"narrate": False})
    for _ in range(100):
        if client.get("/timeline/status").json()["ready"]:
            break
        time.sleep(0.1)
    snap = client.get("/timeline", params={"at": "08:55"}).json()
    assert snap["time"] == "08:55"
    assert snap["board"]["totals"]["rostered"] == 24
    assert snap["alerts"]
    assert snap["events"]
    assert snap["landmarks"]
    for alert in snap["alerts"]:
        assert "actions" in alert


def test_acting_from_the_slider_is_stamped_with_the_stories_clock(client):
    """A decision taken while scrubbing at 08:55 must be recorded at 08:55,
    not at wherever the live replay's cursor happens to sit, and must not be
    visible when the presenter scrubs back to 08:50."""
    import time

    client.post("/timeline/build", params={"narrate": False})
    for _ in range(100):
        if client.get("/timeline/status").json()["ready"]:
            break
        time.sleep(0.1)

    snap = client.get("/timeline", params={"at": "08:55"}).json()
    billing = next(a for a in snap["alerts"] if a["queue"] == "billing")
    done = client.post(
        f"/alerts/{billing['id']}/act",
        params={"pathway": "EARLY_SHIFT_COVER", "at": "08:55"},
    ).json()
    assert done["status"] == "recorded"
    assert done["sent_at"].endswith("08:55:00")

    before = client.get("/timeline", params={"at": "08:50"}).json()
    after = client.get("/timeline", params={"at": "09:00"}).json()
    assert sum(len(a["actions"]) for a in before["alerts"]) == 0
    assert sum(len(a["actions"]) for a in after["alerts"]) == 1


def test_the_live_board_still_works_after_a_build(client):
    """Building drives the session to the end of the morning. Start over and
    the live controls must still behave."""
    import time

    client.post("/timeline/build", params={"narrate": False})
    for _ in range(100):
        if client.get("/timeline/status").json()["ready"]:
            break
        time.sleep(0.1)
    client.post("/replay/reset")
    client.post("/replay/start", params={"to": "08:30"})
    assert client.get("/board").json()["time"] == "08:30"
