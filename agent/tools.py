"""What the agent is allowed to do.

Six tools, and the boundary they draw is the whole design. Everything on the
other side of it, every projection, every service level, every cost, is already
computed by the time the model sees anything. The tools hand over finished
numbers and take back sentences.

That split is not squeamishness about language models. It is that the manager
forwards these figures to their director, so the figures have to be
reproducible, auditable, and identical between two options being compared. A
model asked to do queueing theory in its head would be slower, more expensive
and wrong in ways nobody could trace.

What the model is genuinely better at is the part the deterministic layer
cannot do: deciding which two facts out of eleven matter this morning, and
writing a message a human will actually send. That is what it is asked for.

Every tool reads the shift bound by `sessions.bind_session`, so the agent
cannot reach another tenant's data even if it tries. Scope is not a parameter
it can set.
"""

from __future__ import annotations

import json
import re
from typing import Any

from app.core.context import context_facts
from app.core.remediation import candidates as find_candidates
from app.sessions import SessionMissing, current_session
from app import store

_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")


def _ungrounded_figures(text: str, payload: dict[str, Any]) -> list[str]:
    """Figures in `text` that were not in what the model was handed.

    The instruction tells the model never to state a number the tools did not
    return. This is where that becomes true rather than requested. A prompt rule
    holds most of the time, and "most of the time" is not a property worth
    offering a manager who forwards these figures to their own director.

    Rejecting is deliberately the safe direction. A false positive costs the
    prose and the board falls back to the computed summary, which is correct but
    plainer. A false negative puts an invented service level on screen in the
    same typeface as a real one, and nobody downstream can tell which is which.
    """
    allowed: set[str] = set()
    for token in _NUMBER.findall(json.dumps(payload, default=str)):
        plain = token.replace(",", "")
        allowed.add(plain)
        # 67.0 and 67 are the same claim, as are 1,700 and 1700.
        allowed.add(plain.split(".")[0])
        try:
            allowed.add(str(round(float(plain))))
        except ValueError:
            pass

    invented = []
    for token in _NUMBER.findall(text):
        plain = token.replace(",", "")
        if plain in allowed or plain.split(".")[0] in allowed:
            continue
        invented.append(token)
    return invented


def _fail(message: str) -> dict[str, Any]:
    """Errors come back as data, not exceptions.

    A raised exception inside a tool aborts the agent's turn and loses the
    conversation. Returning the problem lets the model recover: it can pick a
    different tool, ask a clarifying question, or tell the manager plainly that
    something is unavailable.
    """
    return {"status": "error", "error": message}


def get_shift_board() -> dict[str, Any]:
    """Current state of the shift: clock, queues, staffing and service level.

    Use this to answer any question about who is in, who is late, how the
    queues are staffed, or what the service level looks like right now.

    Returns:
        dict with 'status', the clock, floor totals, and per-queue staffing,
        impact and rider detail.
    """
    try:
        session = current_session()
    except SessionMissing as exc:
        return _fail(str(exc))

    board = session.replay.board()
    # Trim the rider list to what a conversation needs. The full board carries
    # projection bases and spreads for the UI; sending all of it on every turn
    # would triple the prompt for detail nobody asks about.
    for queue in board["queues"]:
        queue["riders"] = [
            {
                "name": r["name"],
                "state": r["state"],
                "eta": (r["eta"] or "")[11:16] or None,
                "minutes_late": r["minutes_late"],
                "vendor": r["vendor"],
            }
            for r in queue["riders"]
        ]
    return {"status": "success", "board": board}


def get_alert(alert_id: int) -> dict[str, Any]:
    """Everything needed to write up one alert, in a single call.

    Includes the cause, the worst-affected riders, the effect on service level
    both now and for the day, how today compares to normal, every option with
    the service level it would produce, and what it costs if nobody acts.

    You do not need to call get_context_facts or get_cover_candidates after
    this. Both are already included.

    Args:
        alert_id: the alert's numeric id.

    Returns:
        dict with 'status' and the alert.
    """
    try:
        session = current_session()
    except SessionMissing as exc:
        return _fail(str(exc))

    found = session.alert_for(alert_id)
    if found is None:
        known = list(session.alert_ids.values())
        return _fail(f"alert {alert_id} is not part of this shift; known ids are {known}")
    _, alert = found
    compact = alert.for_narrative()
    compact["alert_id"] = alert_id
    return {"status": "success", "alert": compact}


def list_alerts() -> dict[str, Any]:
    """Every alert on this shift, open and resolved, with their ids.

    Returns:
        dict with 'status' and a compact list: id, queue, status, cause,
        coverage and service level.
    """
    try:
        session = current_session()
    except SessionMissing as exc:
        return _fail(str(exc))

    session.persist()
    out = []
    for queue, alert in session.replay.alerts.items():
        sl = (alert.impact or {}).get("service_level") or {}
        out.append(
            {
                "id": session.id_for(queue),
                "queue": queue,
                "queue_name": alert.display_name,
                "status": alert.status.value,
                "cause": alert.cause.value,
                "coverage_pct": alert.coverage_pct,
                "service_level_pct": sl.get("service_level_pct"),
                "narrated": alert.narrative is not None,
            }
        )
    return {"status": "success", "clock": session.replay.now.strftime("%H:%M"), "alerts": out}


def get_cover_candidates(queue: str, limit: int = 3) -> dict[str, Any]:
    """Who could cover a short-staffed queue right now.

    Only returns people whose arrival is already recorded, so every suggestion
    is somebody verifiably in the building. Ranked so that whoever has covered
    least this week is asked first.

    Args:
        queue: which queue needs cover. Use the short key from the board,
            'billing' or 'techsupport', not the display name.
        limit: how many names to return. Defaults to 3.

    Returns:
        dict with 'status' and a list of candidates, each with a name, their
        queue, when they arrived, and how much cover they have already done.
    """
    try:
        session = current_session()
    except SessionMissing as exc:
        return _fail(str(exc))

    replay = session.replay
    known = {q["queue"] for q in replay.queue_rows}
    if queue not in known:
        return _fail(f"unknown queue {queue!r}; this site runs {sorted(known)}")

    people = find_candidates(
        queue, replay.shift_date, replay.now, replay.office, limit=max(1, min(limit, 8))
    )
    return {
        "status": "success",
        "queue": queue,
        "candidates": [c.as_dict() for c in people],
    }


def get_context_facts(queue: str) -> dict[str, Any]:
    """How today compares to normal for this team, site and weekday.

    This is what turns a number into a judgement. Use it before writing any
    summary, so the manager is told whether the morning is unusual rather than
    just what the morning is.

    Args:
        queue: the queue to benchmark. Use 'billing' or 'techsupport'.

    Returns:
        dict with 'status' and a list of facts, each with its own sample size.
    """
    try:
        session = current_session()
    except SessionMissing as exc:
        return _fail(str(exc))

    replay = session.replay
    alert = replay.alerts.get(queue)
    late_today = None
    if alert:
        late_today = len(alert.riders_affected)

    facts = context_facts(
        replay.office, replay.shift_type, replay.shift_date, queue, late_today=late_today
    )
    return {"status": "success", "facts": [f.as_dict() for f in facts]}


def compose_alert(
    alert_id: int,
    narrative: str,
    cover_draft: str = "",
    transport_draft: str = "",
    operations_draft: str = "",
) -> dict[str, Any]:
    """Attach a written summary and ready-to-send drafts to an alert.

    This is the acting step: the summary and drafts are saved against the alert
    and appear on the manager's board immediately. Write the narrative in at
    most five sentences covering who is affected, the impact on service level,
    one comparison to normal, and the recommended action. Use only names and
    figures returned by the other tools.

    Args:
        alert_id: the alert to write up.
        narrative: the summary for the line manager, at most five sentences.
        cover_draft: message to the early-shift lead requesting cover, if cover
            is being recommended. Leave empty otherwise.
        transport_draft: message to the transport manager, only when several
            affected riders share one vendor. Leave empty otherwise.
        operations_draft: message to the operations head, only when the day's
            service level target is at risk. Leave empty otherwise.

    Returns:
        dict with 'status' confirming what was saved.
    """
    try:
        session = current_session()
    except SessionMissing as exc:
        return _fail(str(exc))

    found = session.alert_for(alert_id)
    if found is None:
        return _fail(f"alert {alert_id} is not part of this shift")
    queue, alert = found

    # Only keep a draft for a pathway the alert actually offers. Without this
    # guard the model's escalation text landed under both ESCALATE_TRANSPORT
    # and ESCALATE_OPS, so a note about one vendor's cabs would have gone to
    # the operations director as if the day's target were at risk.
    offered = {o.pathway.value for o in alert.options}
    proposed = {
        "EARLY_SHIFT_COVER": cover_draft.strip(),
        "ESCALATE_TRANSPORT": transport_draft.strip(),
        "ESCALATE_OPS": operations_draft.strip(),
    }
    drafts = {k: v for k, v in proposed.items() if v and k in offered}
    ignored = sorted(k for k, v in proposed.items() if v and k not in offered)

    # Check the prose against the figures the model was actually given, before
    # any of it is saved. The drafts are checked with it: a cover request naming
    # an overtime cost nobody computed is forwarded to a real person.
    grounding = alert.for_narrative() | {"alert_id": alert_id}
    invented = _ungrounded_figures(" ".join([narrative, *drafts.values()]), grounding)
    if invented:
        return _fail(
            f"nothing was saved: the text states {', '.join(sorted(set(invented)))}, "
            f"which get_alert did not return. Every figure must come from "
            f"get_alert. Rewrite the sentence around the figures get_alert gave "
            f"you, and call compose_alert again. Do not delete the figure and "
            f"leave the sentence otherwise intact."
        )

    # A model told its figure was wrong sometimes deletes the figure rather than
    # correcting it, which leaves "four agents short until ." on the board. An
    # empty slot reads as a bug to the manager and is worse than the computed
    # summary the board falls back to, so it is refused as well.
    if re.search(r"\s[.,;%]|\s{2,}", narrative):
        return _fail(
            "nothing was saved: the summary has a gap in it where a figure "
            "should be. Write the sentence with the figure from get_alert in "
            "place, or write a different sentence that does not need it."
        )

    # Observed on a small local model: it wrote the call it was supposed to make
    # as the text of the argument, and `compose_alert(alert_id=..., narrative="`
    # rendered on the board as the morning's summary. A manager's summary never
    # contains a function call, so this costs nothing and catches a whole family
    # of malformed turns.
    if re.search(r"\b(compose_alert|get_alert|record_action|get_shift_board)\s*\(", narrative):
        return _fail(
            "nothing was saved: the summary contains a function call instead of "
            "prose. Send only the sentences the manager should read as the "
            "narrative argument."
        )

    alert.narrative = narrative.strip()
    alert.drafts = drafts
    alert.mark_narrated(session.replay.now)

    row_id = store.save_alert(alert)
    session.alert_ids[queue] = row_id
    # Memoise against the situation, not the shift, so the next morning with
    # the same shape, or the next rehearsal of this one, costs nothing.
    store.remember_narrative(alert.payload_hash(), alert.narrative, drafts)
    result = {
        "status": "success",
        "alert_id": row_id,
        "queue": queue,
        "narrative_saved": bool(alert.narrative),
        "drafts_saved": sorted(drafts),
    }
    if ignored:
        result["drafts_ignored"] = ignored
        result["note"] = (
            f"{', '.join(ignored)} is not an option on this alert, so that draft "
            f"was discarded. Offered: {sorted(offered)}."
        )
    return result


def record_action(alert_id: int, pathway: str) -> dict[str, Any]:
    """Record that the manager chose a course of action on an alert.

    Only call this when the manager has actually decided something. Suggesting
    an option is not the same as taking it, and this writes to the audit trail
    the manager may later have to defend.

    Args:
        alert_id: the alert being acted on.
        pathway: one of HOLD_OVER, EARLY_SHIFT_COVER, CROSS_COVER,
            CONTACT_EMPLOYEE, ESCALATE_TRANSPORT, ESCALATE_OPS, WAIT.

    Returns:
        dict with 'status' and the draft message that was sent.
    """
    try:
        session = current_session()
    except SessionMissing as exc:
        return _fail(str(exc))

    found = session.alert_for(alert_id)
    if found is None:
        return _fail(f"alert {alert_id} is not part of this shift")
    _, alert = found

    option = next((o for o in alert.options if o.pathway.value == pathway), None)
    if option is None:
        offered = [o.pathway.value for o in alert.options]
        return _fail(f"{pathway} is not offered on this alert; options are {offered}")

    draft = alert.drafts.get(pathway, "")
    action = store.record_action(
        alert_id=alert_id,
        pathway=pathway,
        draft=draft or None,
        people=option.people,
        at=session.replay.now,
        cost=option.cost,
    )
    return {
        "status": "success",
        "pathway": pathway,
        "draft": draft,
        "people": [p.get("name") for p in option.people if p.get("name")],
        "recorded_at": action["sent_at"].strftime("%H:%M"),
    }


ALL_TOOLS = [
    get_shift_board,
    list_alerts,
    get_alert,
    get_cover_candidates,
    get_context_facts,
    compose_alert,
    record_action,
]

NARRATOR_TOOLS = [get_alert, compose_alert]
"""What the write-up agent gets. Tool schemas are resent on every request, so
handing a single-purpose agent the full set is a bill for capability it will
never use, and an invitation to reach for the wrong tool."""
