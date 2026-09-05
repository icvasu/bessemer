# Shift Readiness Agent — Implementation Plan

Persona: **line manager**, BPO support floor. One site, one shift, two queues.
Hackathon budget: **8 hours**. Plan uses 7.75 h, leaving 15 min slack. Cut lines are mandatory, not optional.

## What we are building

A shift-readiness agent that watches every rostered agent's inbound commute, projects floor staffing per queue at shift start, and when a threshold breaks, tells the line manager who is late, what it costs the queue, and what to do, with the messages already drafted.

- **Sense**: a replay clock advances over trip data in Postgres and emits events (cab started, picked up, dropped, no pickup past plan, alert raised).
- **Reason**: rider state + ETA, queue projection, impact, baseline context, threshold, cause, pathways, candidates. All deterministic Python over Postgres.
- **Act**: a Google ADK agent receives the alert payload, calls tools to fetch cover candidates and context, and writes the narrative plus drafted messages back to Postgres. The same agent answers the manager's questions in chat.

Demo slice: `pinnacle-Slc` / `Clearwater Campus` / `09:00` shift. 24 riders who rode 40+ days. Demo day `2026-06-11` (Thu). Baseline: typical day 14 of 24 late (5 min grace makes it 16 on demo day).

## Stack (decided)

- **Database**: PostgreSQL 18, already running locally on `127.0.0.1:5432`, user `postgres`, no password. Database name `bessemer`. Holds the normalised dataset, the roster, alerts, actions, and ADK sessions.
- **Backend**: Python 3.12 in `.venv/`, `psycopg` (v3), pandas for the one-time load, FastAPI for the board and replay control.
- **Agent**: Google ADK (`google-adk`), `gemini-2.5-flash`, `DatabaseSessionService` pointed at the same Postgres. Needs `GOOGLE_API_KEY` from AI Studio in `agent/.env`. Not set on this machine yet. Swapping the model to Claude is one line through ADK's `LiteLlm` wrapper if wanted later.
- **Frontend**: Vite + React single page, polling the API every tick. Plain HTML + fetch if time is short.
- Hackathon says "preferably Java, Angular, AWS" but "not restrictive". Deployability slide: Postgres and a stateless Python service, ADK deploys to Cloud Run with one command, and every table is keyed by `business_unit`.

## Repo layout

```
bessemer/
  MoveInSync - Anonymised Trip-Log Dataset/   # raw, never modified, gitignored
  db/
    schema.sql         # tables, views, indexes
    load.py            # CSV -> normalise in pandas -> COPY into Postgres
    seed_roster.py     # writes roster + queues tables
  app/
    config.py          # DB URL, defaults (business_unit, office, shift)
    db.py              # psycopg connection helpers
    core/
      state.py         # rider state machine
      eta.py           # P75 travel-time lookup + projection
      queue.py         # staffing projection + impact
      context.py       # baselines from views
      alerts.py        # Cause enum, thresholds, lifecycle, pathways_for()
      remediation.py   # candidate search: 08:30 shift, adjacent queue
    replay.py          # clock + event stream, one shift-day in memory
    api.py             # FastAPI: board, alerts, replay control, chat
  agent/
    __init__.py
    agent.py           # root_agent = Agent(...), tools wrap app.core + db
    tools.py
    .env               # GOOGLE_API_KEY
  web/                 # frontend
  tests/
  scratch/             # throwaway
  PLAN.md
  README.md
```

---

## Phase 1 — Postgres foundation (1.25 h)

Goal: the dataset lives in Postgres, normalised once, plus the roster.

1. `db/schema.sql` (20 min). Dataset tables:
   - `trips` (one row per trip, 3 months unioned): `trip_id bigint PK`, `business_unit`, `office`, `product_type`, `trip_date date`, `shift_type`, `trip_direction`, `vendor_id`, `planned_start`, `planned_end`, `actual_start`, `actual_end` (all `timestamp`), `delay_reason`, `delay_minutes int`, `trip_nodal`, `planned_cnt`, `actual_cnt`, `noshow_cnt`.
   - `rider_legs` (one row per rider per trip): `id bigserial PK`, `trip_id`, `stwid bigint`, `business_unit`, `office`, `trip_date date`, `shift_type`, `planned_pickup`, `planned_drop`, `actual_pickup`, `actual_drop` (nullable `timestamp`), `signintype`, `emp_role`, `boarding_status`, `not_boarding_reason`, `is_no_show bool`. Index on `(office, shift_type, trip_date)` and on `stwid`.
   - `trip_alerts`: `event_id uuid PK`, `trip_id`, `stwid`, `event_type`, `start_time`, `ack_time`, `state`, `severity` (the stray `"False"` becomes null), `source`.
   - Skip `bill_data` and `trip_feedback`. Not used by this persona.

   App tables:
   - `queues`: `queue PK`, `aht_min`, `calls_per_30min`, `sl_target`, `line_manager`, `early_shift_lead`, `transport_manager`.
   - `roster`: `stwid PK`, `display_name`, `shift_type`, `queue`, `role` (`primary` | `cover`).
   - `shift_alerts`: `id`, `business_unit`, `office`, `shift_date`, `shift_type`, `queue`, `status`, `cause`, `payload jsonb`, `payload_hash`, `narrative`, `drafts jsonb`, `opened_at`, `updated_at`, `resolved_at`.
   - `alert_actions`: `id`, `alert_id`, `pathway`, `draft`, `sent_at`.
   - `cover_log`: `stwid`, `iso_week`, `minutes`.

   Views:
   - `v_login_legs`: `rider_legs` join `trips`, `trip_direction = 'LOGIN'`, `stwid <> 0`, `emp_role in ('employee','projectmgr')`, `signintype = 'Planned'`, `shift_type ~ '^\d\d:\d\d$'`. Adds `shift_start = trip_date + shift_type`, `late_min = actual_drop - shift_start`.
   - `v_travel_time`: P50 and P75 of `actual_drop - actual_pickup` grouped by `office`, `trip_nodal`, pickup half-hour.
   - `v_shift_baseline`: late share by `office`, `shift_type`, day of week.

2. `db/load.py` (35 min). Read each CSV with pandas, apply the known fixes (strip commas from ids, epochs, `delay_minutes`; reconcile `is_driver_nc` / `planned_km` dtypes across months; clip negative km to null; parse the three date formats), then stream into Postgres with `cursor.copy("COPY ... FROM STDIN")`. Log rows rejected and why. Expect about 2 min for 2.3M rows.
3. `db/seed_roster.py` (15 min). Picks the 24 primary stwids (already known) and 24 cover stwids from the 08:30 shift at Clearwater who rode 40+ days. Splits both into `billing` (12) and `techsupport` (12). Queue params: `aht_min` 5, `calls_per_30min` 36 and 30, `sl_target` `80/20`. Manager names are placeholders.
4. `.gitignore`: dataset, `.venv`, `agent/.env`, `scratch/`.

**Done when**:
```
psql -h 127.0.0.1 -U postgres -d bessemer -c "select count(*) from v_login_legs where office='Clearwater Campus' and shift_type='09:00'"
```
returns about 39k, and `select * from v_travel_time where office='Clearwater Campus'` returns rows.

**Cut line**: if past 1.5 h, skip `trip_alerts` entirely.

---

## Phase 2 — Reasoning core (2 h)

Goal: every decision the agent makes, as pure functions, tested on 2026-06-11. Reads Postgres once per shift-day into a DataFrame; no per-tick queries.

1. `state.py`: `rider_state(row, now) -> (State, eta | None)`. States: `SCHEDULED`, `CAB_MOVING`, `CAB_LATE` (planned start passed, no start), `PICKED_UP`, `DROPPED`, `NO_PICKUP` (planned pickup + 5 min passed, no pickup), `NO_SHOW` (`boarding_status = Not Boarded`), `CANCELLED` (`not_boarding_reason = TRIP_CANCELLED_FROM_DASHBOARD`). Uses only columns visible at `now`.
2. `eta.py`: loads `v_travel_time` for the office once. `project_arrival(row, now)`: if picked up, pickup + P75; if not, planned pickup + max(0, cab delay) + P75. If planned drop passed with no drop, return `None` and flag `ETA_UNKNOWN`.
3. `queue.py`: `projection(riders, now, targets=[09:00, 09:15, 09:30])` per queue: `{planned, on_floor, in_transit, at_risk, absent}`. `impact(queue, t)`: `{agents_missing, minutes_lost, calls_unanswered, sl_estimate}` with `calls_unanswered = minutes_lost / aht_min`.
4. `context.py`: three facts from `v_shift_baseline` and `v_login_legs`: this team's typical late count, today's weekday rate vs the best weekday, same weekday last week.
5. `alerts.py`: `Cause` enum `CAB_NOT_STARTED`, `NO_PICKUP`, `CANCELLED`, `PICKED_UP_LATE`, `EN_ROUTE_DELAY`, `PATTERN`. `classify(rider, now)`. Thresholds: queue projected < 75% at 09:00, or any rider ETA > 09:15, or any `NO_PICKUP`. Lifecycle `OPEN -> UPDATED -> RESOLVED`, one alert per queue per shift, persisted in `shift_alerts`, resolved when the queue reaches plan. `pathways_for(cause, impact, candidates)` from the fixed set `WAIT`, `EARLY_SHIFT_COVER`, `CROSS_COVER`, `CONTACT_EMPLOYEE`, `ESCALATE_TRANSPORT`.
6. `remediation.py`: `candidates(queue, shift_date, now, n=3)`. Query: `roster.role = 'cover'` and same queue, joined to that day's `v_login_legs`, keep `actual_drop <= now` (verifiably on floor), order by `cover_log` minutes this week ascending. Then the adjacent queue's cover riders. Returns names, stwids, drop time.
7. `tests/test_core.py`: riders 94535, 602365, 480600 on 2026-06-11 at 08:00, 08:15, 08:30, 08:45, 08:55, 09:00, 09:15. Assert states and that the billing alert opens at the first tick where projection < 75%.

**Done when**: `python -m app.replay --date 2026-06-11 --print` prints the state table at each tick and the alert payload as JSON. No model involved.

**Cut line**: at 3.25 h elapsed, drop `CROSS_COVER` and the "same weekday last week" fact.

---

## Phase 3 — Replay engine + API (1 h)

Goal: the core runs on a clock, reachable over HTTP, persisting alerts to Postgres.

1. `replay.py`: `Replay(office, shift_date, shift_type, start=07:30, end=09:45, tick=60s, speed=60x)`. Loads the day's legs once. Each tick: recompute states, projection, alerts. Writes alert transitions to `shift_alerts` and appends to an in-memory event log. On `OPEN`, and on `UPDATED` when the affected rider set changes, calls `agent.compose(alert_id)` (Phase 4). If the agent is unavailable, the alert still persists with `narrative = null`.
2. `api.py`:
   - `POST /replay/start?date=&speed=`, `POST /replay/reset`
   - `GET /board` → clock, queues with projection and impact, riders with state and ETA
   - `GET /alerts` → open and resolved alerts with payload, narrative, drafts, candidates
   - `GET /events?since=`
   - `POST /alerts/{id}/act?pathway=` → writes `alert_actions`, increments `cover_log` for named candidates, returns the draft
   - `POST /chat` → forwards to the ADK runner (Phase 4)
3. Every endpoint takes `business_unit` and `office`, defaulted from config. Multi-tenancy made true.

**Done when**: `curl localhost:8000/board` returns the board mid-replay, and `select * from shift_alerts` shows the billing alert opened at 08:55.

---

## Phase 4 — ADK agent (1.25 h)

Goal: alerts carry a narrative and ready-to-send drafts, written by an agent that fetched the facts itself. The same agent answers questions.

1. `agent/tools.py` (30 min). Thin wrappers over `app.core` and `app.db`, each returning a dict:
   - `get_shift_board(office, shift_date, shift_type, now)` → queues, riders, states, ETAs
   - `get_alert(alert_id)` → payload
   - `get_cover_candidates(queue, shift_date, now, n)` → from `remediation.candidates`
   - `get_context_facts(office, shift_type, shift_date)` → from `context`
   - `compose_alert(alert_id, narrative, cover_draft, transport_draft)` → writes `narrative` and `drafts` to `shift_alerts`. This is the act.
   - `record_action(alert_id, pathway)` → writes `alert_actions`
2. `agent/agent.py` (20 min). `root_agent = Agent(model="gemini-2.5-flash", name="shift_readiness", tools=[...])`. Instruction: you are the line manager's shift-readiness assistant; when given an alert payload, call `get_context_facts` and `get_cover_candidates`, then `compose_alert` with a narrative of at most 5 sentences (who is late, worst ETA, impact, one context fact, recommended pathway), a cover draft to the early shift lead naming candidates and minutes, and a transport draft only when 2+ late riders share a vendor. When asked a question, use `get_shift_board`. Never invent names or times; only use tool output. Do not set `output_schema`, it disables tools; `compose_alert` is the structured output.
3. Runner wiring (20 min). In `api.py`: `Runner(agent=root_agent, app_name="bessemer", session_service=DatabaseSessionService(db_url="postgresql+psycopg://postgres@127.0.0.1:5432/bessemer"))`. One session per shift-day: `session_id = f"{office}:{shift_date}:{shift_type}"`, `user_id = line_manager`. Alert triggers and chat share the session, so the agent knows what it already told the manager. `compose(alert_id)` sends the message `"Alert {status}: {payload json}. Compose the alert."` and awaits the final event.
4. Cache (10 min). Before invoking the agent, look up `shift_alerts` by `payload_hash`; if a composed alert with the same hash exists, copy its narrative and drafts. Replays during the demo then cost zero model calls.
5. `adk web agent/` for debugging tool calls (5 min to verify it starts).

**Done when**: `/alerts` shows `narrative` and `drafts` populated for the billing alert, and `POST /chat {"text": "who is late on billing?"}` answers from `get_shift_board`. The ADK `events` table in Postgres shows the tool calls.

**Cost line for the deck**: about 3 model turns per alert (fetch facts, compose, confirm), roughly 6k tokens total. 2 to 5 alerts per team per shift. Per-tick processing never touches a model. Look up current Flash pricing for the dollar figure; do not guess it.

**Cut line**: at 5.75 h elapsed, drop `/chat` and keep the trigger path only.

---

## Phase 5 — UI, demo, deliverables (2.25 h)

Goal: a 3-minute demo that runs end to end, plus the artefacts the judges asked for.

1. **Shift board** (1.25 h). One screen. Replay clock and speed at top. Two queue cards: planned / on floor / in transit / at risk / absent, impact line underneath. Rider table: name, queue, state, ETA, vendor. Alert panel on the right: narrative, context fact, candidates, pathways as buttons. Clicking shows the draft in a "sent" log. Chat box under the alert panel if `/chat` survived.
2. **Demo script** (10 min). Six beats:
   1. 07:45 board green, 24 planned.
   2. 08:12 cab for rider 602365 started 15 min late, rider flagged at risk.
   3. 08:43 rider 480600 no pickup, no-show suspected, contact-employee pathway shown.
   4. 08:55 billing projected 50% at 09:00, alert opens, narrative reads out, cover draft names three 08:30-shift agents.
   5. Click cover. Draft appears in sent log, `alert_actions` row written.
   6. 09:00 to 09:30 arrivals stream in, impact counter falls, alert auto-resolves at 09:24.
3. **README** (20 min): Postgres setup, load command, `GOOGLE_API_KEY`, run, the six beats, data quirks handled.
4. **Architecture diagram** (15 min): mermaid. CSV → `db/load.py` → Postgres → replay clock → reasoning core → `shift_alerts` → ADK agent (tools) → narrative + drafts → API → board. Mark where the model sits and where it does not.
5. **Deck outline** (15 min): problem, persona, the NODELAY finding, the loop, deterministic vs agent split, cost at scale, multi-tenancy, what is synthetic, what we cut.

**Done when**: a teammate who has not seen the code runs the README and reaches beat 4 in under 5 minutes.

**Cut line**: at 7.25 h elapsed, drop the event feed, speed control and chat box. Keep board, alert panel, one button.

---

## What is real and what is synthetic

| Thing | Source |
|---|---|
| Riders, shifts, trips, pickup and drop times, no-shows, vendors | real, from the dataset |
| Cover pool membership (which 08:30-shift riders exist and when they arrived) | real, from the dataset |
| Which queue a rider belongs to | synthetic, `roster` table |
| Handle time, call forecast, service level target | synthetic, `queues` table |
| Cover minutes this week | synthetic counter, starts at 0, incremented by manager clicks |
| Manager names | placeholders |

Not modelled, say so on a slide: skills beyond queue membership, breaks, contractual overtime rules, vendor contact.

## Requirement mapping

| Requirement | Where |
|---|---|
| Working prototype on the dataset | Phases 1 to 5 |
| Agentic: senses, reasons, acts | replay (sense), core (reason), ADK agent writing drafts (act) |
| Serves one persona | line manager |
| Contextualises against a reference point | `context.py` over `v_shift_baseline` |
| Combines 2+ solution forms | proactive alerting + automated comms + dashboard + conversational agent |
| Handles messy data | `db/load.py`, rejects logged |
| Proactive triggers | thresholds fire on the clock |
| Deployability | Postgres, `business_unit` on every table, ADK to Cloud Run, zero-token ticks |

## Risks

1. **ETA is optimistic without GPS.** P75 travel time plus the planned-drop-passed hard trigger.
2. **No `GOOGLE_API_KEY` on this machine yet.** Get an AI Studio key before Phase 4. Phase 3's fallback (`narrative = null`) keeps the demo alive without it.
3. **ADK tool loop latency during the demo.** Payload-hash cache in `shift_alerts`; run the replay once before presenting.
4. **Clearwater has no outgoing shift at 09:00.** Checked. Cover comes from the 08:30 shift, whose `DROPPED` state proves presence.
5. **Time.** 15 min slack. Honour every cut line.

## Out of scope, say so on a slide

Overtime approval and HR rules. Contacting vendors. Maps and GPS. Authentication. The other two personas.
