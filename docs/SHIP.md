# Ship path — Shift Readiness Agent

For a MoveInSync partner, judge, or integrator. Short and honest.

## What this is for MoveInSync

MoveInSync already knows who is on which cab, when pickup and drop were planned, and when a trip ran late. Line managers still get a GPS-style delay flag, not an answer they can use on the floor. This agent sits on that trip feed and answers one question at ten to nine: **is my queue staffed at shift start, what does a miss cost on the contract, and what do I do — with the message already written.**

## How it helps their business

- **Keep BPO and 24/7 enterprise clients.** Those contracts are won on service level and lost on hold-over and overtime. A late cab is their problem only when it becomes an unmanned desk.
- **Turn commute data into floor money and SLA.** Headcount is not linear: four late of twelve can drop service level from ~90% to ~15%. The board shows that number, not a delay badge.
- **Cut overtime and hold-over.** On a 24/7 desk a late arrival pins the night agent past shift end and can miss the cab home. Cover options are scored so the manager can move people who are already on the floor.
- **Give the line manager an action, not a GPS delay flag.** Transport often stamps `NODELAY` on arrivals that still miss the bell. This product never reads that column. It watches the floor and offers a button: move cover, hold over, call the rider, escalate.

## How it plugs into the current product

Do not rewrite this system. Swap the clock.

MoveInSync already has trips, pickup/drop, alerts, and a **line manager** desk (in the sibling product the board is iframed; chat and act stay on this API). Point the same core at their live trip / pickup / drop feed. `app/replay.py` is the only swap: it is a clock over recorded legs. A consumer that advances `now` from live events is the production clock. Rider state, ETA, queues, Erlang, alerts, drafts, and `/board` / `/alerts` / `/act` / `/chat` stay.

The board (`web/index.html`) can sit in their line-manager UI. The ADK model is a one-line swap (`BESSEMER_MODEL` — OpenAI, Gemini, or `ollama_chat/llama3.2`). Fallback drafts still send if the model is down.

Auth, SSO, and multi-region are **not in this build**. Wrap this API the way MoveInSync already wraps a desk: their login, `x-tenant-id` / signed write-backs, and the existing tenant envelope. This process only needs `business_unit` + `office` on every request (already the `Scope` dependency in `app/api.py`).

## Integration sketch

```
their trip / pickup / drop events
        ↓
Replay-equivalent clock   app/replay.py  (only `now` and the day's legs change)
        ↓
core                      app/core/state.py → eta.py → queue.py → sla.py
                          → context.py → remediation.py → alerts.py
        ↓
alerts + options          Postgres: shift_alerts, alert_actions
        ↓
agent                     agent/agent.py  narrator + chat  (tools in agent/tools.py)
        ↓
API                       app/api.py  /board /alerts /events /alerts/{id}/act /chat
        ↓
their UI                  line-manager board / iframe  (same JSON the demo board already reads)
```

Config for tenant and site is `BESSEMER_BU` and `BESSEMER_OFFICE` in `app/config.py`. Sessions are keyed `business_unit|office|date|shift`.

## What is already production-shaped vs demo-only

**Production-shaped (keep):**

- Tenant-scoped API: every board/alert/act/chat call takes `business_unit` + `office` (defaults from config).
- Durable store in Postgres; reasoning core is pure Python, ~15 ms/tick, no model on the clock.
- `/health` reports process, mode, and whether the model key is present.
- Secrets stay in `.env` / `agent/.env` (gitignored). Never commit them.
- Replay is the live-event seam: same `now` guard, same downstream.

**Demo-only (do not ship as fact):**

- **Replay clock** — recorded morning, not a live Kafka/CDC consumer.
- **Night shift and positional handover** — asserted in `db/seed_roster.py` because this site's trip log has no outbound legs near 09:00. Flagged `synthetic` on the roster.
- **Queue AHT, call forecast, SLA target, overtime rate** — synthetic in `queues` and `app/config.py`. Replace with their WFM numbers.
- **Story slider / timeline** — a presenter tool (`app/timeline.py`). Not the product.

Child tables (`alert_actions`, `narrative_cache`, `cover_log`) inherit scope from the alert or the rider, not their own `business_unit` column. Fine for this build; not a second tenancy product.

## 90-second jury / partner pitch

MoveInSync already sees the commute. The line manager sees a delay flag that is often stamped `NODELAY` while the desk is still empty. We replay a real morning on their own trip log. At 08:55 Billing is four short, service level 15%, and the night shift is about to be held. The numbers are arithmetic. The model only writes the sentence and the draft. Click the green button: four people already on the floor, message ready to send, overtime priced. Swap replay for their live trip events and the same API sits in the line-manager desk they already have. That is how commute data becomes SLA and floor dollars — and why a BPO client stays.

## Honest gaps

**Not in this build — wrap with what MoveInSync already has:**

- No login, SSO, or API keys on these routes. Their product already has a signed-in desk and tenant headers.
- No multi-region, sharding, or a live event bus. Their production plan already talks CDC / Kafka for trip events; that feed replaces `Replay`.
- No GPS. Pickup and drop times only. Once a rider is aboard, nothing is visible until they arrive (~13 min spread). Hard trigger is “planned drop passed, no arrival.”
- Erlang C assumes Poisson arrivals, exponential handle times, no abandonment, interchangeable agents. Shape of the answer is what the decision needs; their WFM stack can replace `app/core/sla.py` later.
- Night shift is invented. Do not quote hold-over minutes as measured fact until a real logout roster exists.

**If OpenAI is out of credits (429):** the board still runs; drafts fall back to templates. For local narration set `BESSEMER_MODEL=ollama_chat/llama3.2` in `agent/.env` and keep the server on `127.0.0.1:8000`. Do not put keys in git.
