# Jury script — Shift Readiness Agent

Speak this. Short. Easy words. Do not invent numbers.

## 1. One-sentence pitch

At ten to nine, a line manager asks one thing: is my floor staffed at nine — and if not, who is late, what it costs, and what I do, with the message already written.

## 2. Business problem and money

Late cabs do not just thin a roster. They empty a desk.

Staffing is not a straight line. Lose 4 of 12 on a loaded queue and service level does not fall to two-thirds. It falls from about 92% to 15%. Calls wait. The contract target is 80% answered in 20 seconds.

This is a 24/7 floor. Night people cannot leave until the day person sits down. Hold four of them 19 minutes past the end and four miss the 09:15 cab home. That is overtime, and a bad morning for someone who already worked the night.

The agent prices that hold-over (about 799 on Billing, 1234 on Technical Support) and names who can cover from people already on the floor.

It also says when **not** to escalate. The bad interval collapses. The day can still hold at 92%. Panic operations only when the *day* is at risk. That saves as much money as the alert itself.

Transport often stamps `NODELAY` on a trip that still misses the bell. 70% of late-for-shift arrivals here get that stamp. The floor is short anyway. We never read that column. We watch the desk.

## 3. What we built and who it is for

A shift-readiness board for the **line manager** — not transport, not HR.

They run queues on a 24/7 support floor. They consume the cab service. They do not manage vendors or GPS.

Their 10-to-9 question: is Billing and Tech Support seated at nine? If not, what does it cost on the contract, who do I move, and what do I tell my director?

Built on MoveInSync trip data. Clearwater Campus. 09:00 shift. 11 June 2026.

## 4. Journeys / scenarios

One real morning. The landmarks are read off the event feed:

- 07:40 — first cab fails to leave (Agent 20)
- 08:01 — Technical Support alert opens
- 08:25 — Agent 09 not collected
- 08:47 — Agent 15 not collected
- 08:49 — first arrival (Agent 22)
- 09:00 — shift starts
- 09:05 — grace ends
- 09:19 — Billing back to strength
- 09:55 — Technical Support back to strength

Six beats we walk: 07:45 green → 08:12 first amber → 08:25 Billing alert → 08:47 “call the rider” → **08:55 the crunch** → 09:19 it heals.

**Act path:** click an option (the green one is “move the early shift onto the queue”) → a draft appears, ready to send → the cost is charged to whoever absorbs it. Four names: Agent 27, 29, 35, 31. They are already on the floor.

Then ask chat: *Who covered this morning, and how much overtime did we avoid?*

## 5. Architecture (say this in under a minute)

Put `docs/architecture.png` on screen and walk it left to right. The three numbered acts on the diagram are the three paragraphs below.

Trip logs live in Postgres. Views do the history maths.

A replay clock is “now”. Nothing downstream can see the future.

Each tick, Python does the sums: who is late, when they land, how short each queue is, Erlang C service level, hold-over cost, who can cover. About 15 milliseconds. No model.

When a threshold breaks, an alert opens with scored options.

Only then does a Google ADK agent, through LiteLLM, write the note and the drafts. Same agent answers chat. Tools hand it computed facts. It is not allowed to invent a number or a name.

The board is one HTML file, served by FastAPI at port 8000. The manager only sees that.

Swap the clock for a live trip feed later. Nothing else changes.

## 6. Code-level flow

**One tick**

1. `db/schema.sql` and `db/load.py` put real trips in Postgres. `db/seed_roster.py` picks the 24-person team and cover pool.
2. `app/replay.py` advances `now`.
3. `app/core/state.py` — nine-state rider machine, only what `now` can see.
4. `app/core/eta.py` — arrival band. `app/core/queue.py` — headcount. `app/core/sla.py` — Erlang C and the day rollup.
5. `app/core/context.py` — benchmarks. `app/core/remediation.py` — cover people, hold-over price.
6. `app/core/alerts.py` — open / update / resolve, options scored on the contract.
7. `app/api.py` serves `/board`. `web/index.html` polls once a second while the clock runs.

**One chat question**

1. Manager types in `web/index.html`.
2. `POST /chat?stream=1` in `app/api.py` streams tokens as they land.
3. `agent/runner.py` and `agent/agent.py` call tools in `agent/tools.py`.
4. Those tools read the same computed board. The model writes sentences. Figures stay in Python.

Green is arithmetic. Purple is prose.

## 7. What is real vs synthetic

**Real:** riders, trips, pickup and drop times, no-shows, vendors, cabs. The 24-person team and the 24-person cover pool are real riders who took this shift 40+ days.

**Synthetic (we say so):** which queue a person serves, handle time, call forecast, service target. The night shift and handover — Clearwater’s log has no outbound cabs near 09:00, so we assert a night desk, because that is what makes a late arrival cost overtime. Overtime rate and cab-home time are in `app/config.py`.

## 8. How to demo in 90 seconds

Open http://127.0.0.1:8000.

1. Press **Jump to 08:55**. Read the Billing note out loud: four short until 09:19, service level 15%, day still holds, four night people stuck, four miss the 09:15 cab.
2. Point at the numbers. Say: every figure is computed. The model only wrote the sentences.
3. Click the green button: **Move the early shift onto the queue**. Show the draft under Sent. Four real names. Cost assigned.
4. Ask chat one question. Tokens should stream.
5. If you have time, press **Run the morning** or step the story landmarks. At 09:19 Billing heals itself.

If the cloud model is out of credits, the board still runs. Chat and write-ups use the local model. Drafts still send from templates if the model is quiet.
