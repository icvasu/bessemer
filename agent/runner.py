"""Driving the agent, and deciding when it is worth driving at all.

Two entry points. `compose` is the proactive one: the replay notices a
situation worth explaining and asks for prose. `ask` is the reactive one: the
manager types a question. Both run through the same ADK runner and the same
tools; they differ in whether they keep a transcript, for reasons below.

The session store is `DatabaseSessionService` pointed at the same Postgres as
everything else. That is not an aesthetic preference: it means the agent's
conversation, the alerts it wrote and the actions taken on them can be read in
one query, which is what an audit of "why did the floor do that on Thursday"
actually requires.

## Two kinds of conversation

Writing up an alert and answering a question look similar and should not share
a transcript.

An alert write-up is a **one-shot task**. It needs the alert and nothing else.
A first version put it in the same per-shift conversation as chat, on the
reasoning that an agent answering questions should remember what it had already
said. The property was real; the cost was not survivable. Every narration
appended its tool results to a transcript that was replayed on every subsequent
call, so a single alert cost 22,000 prompt tokens and each re-run of the demo
made the next one worse. Compose now runs in a throwaway conversation.

The agent loses nothing by this. It can still see what it wrote, because
`list_alerts` and `get_alert` report the saved narrative. Reading a fact back
from the database costs a few dozen tokens; carrying the transcript that
produced it costs thousands.

**Chat** keeps the persistent per-shift conversation, because there a follow-up
question genuinely depends on the previous answer.

## Not calling the model

The cheapest model call is the one that does not happen, and this module is
mostly about not making them.

* The replay decides whether a situation has changed enough to deserve new
  prose. Ticks that only nudge a number reuse what is already written.
* Before invoking, the narrative cache is checked by payload hash. A morning
  with the same cause, severity band and recommendation as one already written
  up gets that text for free.
* If the model is unreachable, the alert keeps its structured payload and the
  board renders a table. Degraded, not broken.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from google.adk.agents.run_config import RunConfig, StreamingMode
from google.adk.runners import Runner
from google.adk.sessions import DatabaseSessionService
from google.genai import types

from app.config import SQLALCHEMY_URL
from app.core.alerts import Alert
from app.sessions import Session, bind_session, unbind_session
from app import store
from agent.agent import MODEL, missing_credentials, narrator_agent, root_agent
from agent.tools import (
    compose_alert as save_narrative,
    get_cover_candidates,
    get_shift_board,
)

log = logging.getLogger(__name__)

APP_NAME = "bessemer"
INVOCATION_TIMEOUT_S = 60.0
CHAT_TIMEOUT_S = 45.0
"""Headroom under Vercel's 60s cap so a hung model still leaves time for the
board fallback. Compose can use the full minute; chat cannot."""

RETRY_ATTEMPTS = 2
"""One retry, then stop. A call that fails twice is an outage rather than a
blip, and a manager at 08:55 is better served by the structured payload than by
a third wait."""

RETRY_BACKOFF_S = 1.5


class ModelUnavailable(RuntimeError):
    """The model could not be reached, carrying a reason fit to show a person."""


@dataclass
class Usage:
    """What the agent has cost so far, for the cost-at-scale claim.

    Counted rather than asserted. A system that says inference is cheap should
    be able to show the meter.
    """

    calls: int = 0
    cache_hits: int = 0
    failures: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    seconds: float = 0.0
    by_reason: dict[str, int] = field(default_factory=dict)

    def record(self, reason: str, seconds: float, prompt: int, completion: int) -> None:
        self.calls += 1
        self.seconds += seconds
        self.prompt_tokens += prompt
        self.completion_tokens += completion
        self.by_reason[reason] = self.by_reason.get(reason, 0) + 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "model": MODEL,
            "calls": self.calls,
            "cache_hits": self.cache_hits,
            "failures": self.failures,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.prompt_tokens + self.completion_tokens,
            "seconds": round(self.seconds, 1),
            "avg_seconds": round(self.seconds / self.calls, 1) if self.calls else 0.0,
            "by_reason": self.by_reason,
        }


USAGE = Usage()


async def clear_conversations(session: Session, user_id: str = "line_manager") -> int:
    """Delete this shift's agent transcripts.

    Called on replay reset so a demo run twice does not carry the first run's
    conversation into the second, which is how the token cost quietly triples
    between rehearsal and presentation.
    """
    service = session_service()
    prefix = conversation_id(session)
    removed = 0
    try:
        listing = await service.list_sessions(app_name=APP_NAME, user_id=user_id)
        for existing in getattr(listing, "sessions", []):
            if existing.id.startswith(prefix):
                await service.delete_session(
                    app_name=APP_NAME, user_id=user_id, session_id=existing.id
                )
                removed += 1
    except Exception as exc:  # noqa: BLE001 - reset must never fail on cleanup
        log.warning("could not clear agent conversations: %s", exc)
    return removed


_runners: dict[str, Runner] = {}
_session_service: DatabaseSessionService | None = None


def session_service() -> DatabaseSessionService:
    """One ADK session store, shared by both agents and by the app's Postgres."""
    global _session_service
    if _session_service is None:
        _session_service = DatabaseSessionService(db_url=SQLALCHEMY_URL)
    return _session_service


def runner(agent=None) -> Runner:
    """A runner per agent, built once and reused."""
    agent = agent or root_agent
    if agent.name not in _runners:
        _runners[agent.name] = Runner(
            agent=agent, app_name=APP_NAME, session_service=session_service()
        )
    return _runners[agent.name]


def conversation_id(session: Session) -> str:
    """The persistent per-shift conversation, used by chat."""
    replay = session.replay
    return f"{replay.office}:{replay.shift_date}:{replay.shift_type}".replace(" ", "_")


def task_conversation_id(session: Session, alert_id: int) -> str:
    """A throwaway conversation for one alert write-up.

    Keyed by alert, clock, and a unique suffix so a re-narration later in the
    morning starts clean, and two write-ups of the same alert at the same
    minute (jump + story build) do not share an ADK session.
    """
    return f"{conversation_id(session)}:task:{alert_id}:{session.replay.now:%H%M}:{time.time_ns()}"


async def _ensure_conversation(session: Session, user_id: str, sid: str) -> str:
    """Create the named ADK conversation if it does not exist yet."""
    service = session_service()
    existing = await service.get_session(app_name=APP_NAME, user_id=user_id, session_id=sid)
    if existing is None:
        await service.create_session(
            app_name=APP_NAME,
            user_id=user_id,
            session_id=sid,
            state={
                "office": session.replay.office,
                "shift_date": session.replay.shift_date.isoformat(),
                "shift_type": session.replay.shift_type,
                "business_unit": session.replay.business_unit,
            },
        )
    return sid


def _event_text(event) -> str:
    if not event.content or not event.content.parts:
        return ""
    return "".join(p.text for p in event.content.parts if p.text)


async def _iter_events(
    session: Session,
    user_id: str,
    text: str,
    reason: str,
    sid: str | None = None,
    agent=None,
    stream: bool = False,
):
    """Run one agent turn and yield ADK events as they arrive.

    The shift is bound for the duration of the call, so the tools can only
    reach this tenant's data no matter what the model asks for.
    """
    missing = missing_credentials()
    if missing:
        raise ModelUnavailable(missing)

    sid = await _ensure_conversation(session, user_id, sid or conversation_id(session))
    message = types.Content(role="user", parts=[types.Part(text=text)])
    run_config = RunConfig(
        streaming_mode=StreamingMode.SSE if stream else StreamingMode.NONE
    )

    token = bind_session(session)
    started = time.perf_counter()
    prompt_tokens = completion_tokens = 0
    try:
        async for event in runner(agent).run_async(
            user_id=user_id,
            session_id=sid,
            new_message=message,
            run_config=run_config,
        ):
            usage = getattr(event, "usage_metadata", None)
            if usage:
                prompt_tokens += getattr(usage, "prompt_token_count", 0) or 0
                completion_tokens += getattr(usage, "candidates_token_count", 0) or 0
            yield event
    finally:
        unbind_session(token)
        USAGE.record(reason, time.perf_counter() - started, prompt_tokens, completion_tokens)


async def _invoke(
    session: Session,
    user_id: str,
    text: str,
    reason: str,
    sid: str | None = None,
    agent=None,
) -> str:
    """Run one agent turn and return its final text."""
    reply: list[str] = []
    async for event in _iter_events(
        session, user_id, text, reason, sid=sid, agent=agent, stream=False
    ):
        if event.is_final_response():
            piece = _event_text(event)
            if piece:
                reply.append(piece)
    return "\n".join(reply).strip()


async def compose(session: Session, alert: Alert, user_id: str = "line_manager") -> bool:
    """Write up one alert, unless the same situation has been written before.

    Returns True if the alert now carries prose, whether newly written or
    reused. False means the model was unreachable and the board should fall
    back to the structured payload.
    """
    alert_id = session.alert_ids.get(alert.queue)
    if alert_id is None:
        alert_id = store.save_alert(alert)
        session.alert_ids[alert.queue] = alert_id

    cached = store.find_cached_narrative(alert.payload_hash())
    if cached and cached.get("narrative"):
        alert.narrative = cached["narrative"]
        alert.drafts = cached.get("drafts") or {}
        alert.mark_narrated(session.replay.now)
        store.save_alert(alert)
        USAGE.cache_hits += 1
        return True

    # The computed figures travel in the prompt rather than waiting to be
    # fetched. get_alert stays available, but a narrator that has to ask for the
    # payload before it can write spends a round trip carrying the same bytes,
    # and a smaller model frequently loses its way between the two calls: it
    # answers with the text of the call it meant to make, or writes prose from
    # figures it half-remembers. One turn, facts already in hand, is both
    # cheaper and markedly more reliable.
    facts = json.dumps(alert.for_narrative() | {"alert_id": alert_id}, default=str)
    prompt = (
        f"Alert {alert_id} on the {alert.queue} queue has just "
        f"{'opened' if alert.status.value == 'OPEN' else alert.status.value.lower()}"
        f" at {session.replay.now:%H:%M}.\n\n"
        f"These are the computed figures, and the only ones you may state:\n"
        f"{facts}\n\n"
        f"Write it up for the line manager and save it by calling compose_alert "
        f"with alert_id={alert_id}."
    )
    try:
        reply = await asyncio.wait_for(
            _invoke(
                session,
                user_id,
                prompt,
                reason=f"compose:{alert.status.value}",
                sid=task_conversation_id(session, alert_id),
                agent=narrator_agent,
            ),
            timeout=INVOCATION_TIMEOUT_S,
        )
    except Exception as exc:  # noqa: BLE001 - the board must survive any failure here
        USAGE.failures += 1
        log.warning("narrative generation failed for alert %s: %s", alert_id, exc)
        # compose_alert may already have committed prose before the call ran out
        # of time, and prose on the alert is prose the board can show.
        return alert.narrative is not None

    if alert.narrative is None and reply:
        # Smaller models often write the summary and skip compose_alert.
        token = bind_session(session)
        try:
            save_narrative(alert_id, narrative=reply)
        finally:
            unbind_session(token)
            session.persist()

    return alert.narrative is not None


def _unreachable(exc: Exception) -> dict[str, Any]:
    """What to say when the model did not answer.

    An unset key is an operator problem with one specific fix, so saying which
    variable is missing beats reporting a generic outage; anything else is
    genuinely transient and the wording should not overclaim. Either way the
    sentence ends with the part the manager actually needs, which is that the
    numbers on the board are untouched by this.
    """
    detail = (
        str(exc)
        if isinstance(exc, ModelUnavailable)
        else "I could not reach the model just then."
    )
    return {
        "reply": f"{detail} The board and alerts are still live and accurate.",
        "error": str(exc),
    }


def _board_reply(session: Session) -> str:
    """A grounded answer from the computed board when the model is down.

    Same numbers the tools would have returned. Without this, a 429 leaves the
    manager with a shrug; the cover names and service levels are already in
    memory and do not need a model to be read aloud.
    """
    token = bind_session(session)
    try:
        packed = get_shift_board()
        if packed.get("status") != "success":
            return ""
        board = packed["board"]
        clock = session.replay.now.strftime("%H:%M")
        bits: list[str] = []
        for queue in board.get("queues") or []:
            sl = ((queue.get("impact") or {}).get("service_level") or {})
            if sl.get("meets_target") is not False:
                continue
            cover = get_cover_candidates(queue["queue"], 3)
            names = [p["name"] for p in cover.get("candidates") or [] if p.get("name")]
            who = ", ".join(names) if names else "nobody already on the floor"
            bits.append(
                f"{queue.get('display_name') or queue['queue']} is at "
                f"{queue.get('coverage_pct')}% coverage and "
                f"{sl.get('service_level_pct')}% service level against a "
                f"{sl.get('target_pct')}% target. People already on the floor "
                f"who can cover: {who}."
            )
        if bits:
            n = len(bits)
            verb = "is" if n == 1 else "are"
            noun = "queue" if n == 1 else "queues"
            return f"At {clock}, {n} {noun} {verb} breaching SLA. " + " ".join(bits)
        totals = board.get("totals") or {}
        return (
            f"At {clock} the floor is {totals.get('on_floor')} in of "
            f"{totals.get('rostered')} rostered, {totals.get('coverage_pct')}% "
            f"coverage. No queue is breaching its service-level target right now."
        )
    finally:
        unbind_session(token)


def _chat_fallback(session: Session, exc: Exception) -> dict[str, Any]:
    """Prefer the board's own figures over a 'model not ready' shrug."""
    grounded = _board_reply(session)
    if grounded:
        return {"reply": grounded, "error": str(exc), "usage": USAGE.as_dict()}
    return _unreachable(exc)


async def ask(session: Session, text: str, user_id: str = "line_manager") -> dict[str, Any]:
    """Answer a manager's question about the shift."""
    try:
        reply = await asyncio.wait_for(
            _invoke(session, user_id, text, reason="chat"),
            timeout=CHAT_TIMEOUT_S,
        )
    except Exception as exc:  # noqa: BLE001
        USAGE.failures += 1
        log.warning("chat failed: %s", exc)
        return _chat_fallback(session, exc)
    if not reply:
        return {"reply": _board_reply(session) or "No answer came back.", "usage": USAGE.as_dict()}
    return {"reply": reply, "usage": USAGE.as_dict()}


async def ask_stream(session: Session, text: str, user_id: str = "line_manager"):
    """Yield chat progress as dicts so the board can paint tokens as they land."""
    deadline = time.perf_counter() + CHAT_TIMEOUT_S
    seen = ""
    reply = ""
    try:
        async for event in _iter_events(
            session, user_id, text, reason="chat", stream=True
        ):
            if time.perf_counter() > deadline:
                raise TimeoutError(f"model call exceeded {CHAT_TIMEOUT_S:.0f}s")
            calls = event.get_function_calls()
            if calls:
                name = (calls[0].name or "a tool").replace("_", " ")
                yield {"type": "status", "text": f"Checking {name}"}
            piece = _event_text(event)
            if not piece:
                continue
            if event.partial:
                delta = piece[len(seen) :] if piece.startswith(seen) else piece
                seen = piece if piece.startswith(seen) else seen + piece
                if delta:
                    reply = seen
                    yield {"type": "delta", "text": delta}
                continue
            if event.is_final_response():
                reply = piece
                leftover = piece[len(seen) :] if piece.startswith(seen) else (
                    "" if piece == seen else piece
                )
                if leftover:
                    yield {"type": "delta", "text": leftover}
        if not reply:
            reply = _board_reply(session) or "No answer came back."
        yield {"type": "done", "reply": reply, "usage": USAGE.as_dict()}
    except Exception as exc:  # noqa: BLE001
        USAGE.failures += 1
        log.warning("chat failed: %s", exc)
        yield {"type": "done", **_chat_fallback(session, exc)}
