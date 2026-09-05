# Shift Readiness Agent

A line manager at a 24/7 support centre has one question at ten to nine: **is my floor staffed at nine, and if not, what do I do about it?**

This is an agent that answers it. It watches every rostered agent's commute, projects floor readiness and service level per queue, and when a threshold breaks it tells the manager who is late, what it costs on the contract, and what to do, with the messages already written.

Built on MoveInSync's anonymised trip-log dataset for the **team / line manager** persona.

| For judges | |
|---|---|
| Spoken pitch | [docs/JURY.md](docs/JURY.md) |
| How this ships (keep vs wrap) | [docs/SHIP.md](docs/SHIP.md) |
| Sample input / output | [samples/SAMPLE_IO.md](samples/SAMPLE_IO.md) |
| Slides | [docs/deck.md](docs/deck.md) |
| Host it | Railway — [DEPLOY.md](DEPLOY.md) |

**Stack:** Python 3.12 · FastAPI · Postgres · Google ADK + LiteLLM · one HTML board, no frontend build.

**90-second demo:** start the app, press **Jump to 08:55**, read the Billing note, click the green cover button, ask chat *Who covered this morning?*

![The shift board at 08:55](samples/screens/board_0855.png)

## What it does

At 08:55 on 11 June, the board reads:

> Billing Support is four agents short until 09:19. Service level is 15% now, and the day's 80% target still holds. This is tracking towards 5 late, about the usual 5 for this team. Recommend early-shift cover from Agent 27, Agent 29, Agent 35, and Agent 31. If nobody acts, 4 night agents are held 19 min past shift end and 4 would miss the 09:15 cab home.

Every figure in that came from a computation, not from the model. The model chose which facts to lead with and wrote the sentences. Below it are the options as buttons, each scored on service level, with the recommended one in green. Clicking one records the decision, returns a message the manager can forward unedited, and charges the cost to whoever absorbs it.

**Sense.** A replay clock advances over the trip data. Every read is guarded by the clock, so nothing is visible before it happened. Swap the replay for a live event feed and nothing else changes.

**Reason.** Rider state, arrival projection, queue headcount, Erlang C service level, day-level rollup, cause, benchmarks, cover candidates, hold-over cost. All deterministic Python over Postgres. About 15 milliseconds a tick.

**Act.** A Google ADK agent, on an OpenAI model through LiteLLM, receives the computed alert and writes the summary and drafts. The same agent answers the manager's questions. It cannot invent a number or a name because the tools only hand it what was computed.

## Setup

You need **Python 3.12+**, **Postgres** (16+), and [`uv`](https://docs.astral.sh/uv/). The organiser dataset folder belongs in the repo root and is gitignored — do not commit it.

```bash
# 1. Database
psql -h 127.0.0.1 -U postgres -c "CREATE DATABASE bessemer"
psql -h 127.0.0.1 -U postgres -d bessemer -f db/schema.sql

# 2. Dataset (place the MoveInSync folder in the repo root, then)
uv sync
uv run python -m db.load          # about 90 seconds, 2.3M rows
uv run python -m db.seed_roster   # 24 riders, a cover pool, a night shift

# 3. Model key (optional — the board works without it)
echo "OPENAI_API_KEY=sk-..." > agent/.env
# never commit agent/.env

# 4. Run
uv run uvicorn app.api:app --port 8000
open http://localhost:8000
```

Config lives in `app/config.py` and is overridable by environment variable. The demo defaults to `pinnacle-Slc`, `Clearwater Campus`, the `09:00` shift, on `2026-06-11`.

Press **Jump to 08:55** to open on the crunch. Press **Prepare the story** once if you will present with the slider.

```bash
uv run pytest tests/ -m "not live"    # no model calls, ~35s
uv run pytest tests/test_agent.py -m live   # 3 tests against the real model
```

Captured request/response pairs for the submission form: [samples/SAMPLE_IO.md](samples/SAMPLE_IO.md). Raw JSON sits in `samples/` (`api_board_0855.json`, `api_alerts_narrated.json`, `api_action.json`, `api_chat.json`).

## Host it

One FastAPI process serves the API and the board. Pair it with one Postgres. **Railway** is the host that matches this architecture (a long-running process, same as local). Render works the same way. Vercel can run the code but kills the background clock; use Jump to 08:55 there.

**Railway (recommended)**

1. New project at [railway.com/new](https://railway.com/new) → **Deploy from GitHub** → `icvasu/bessemer`.
2. `railway.toml` already sets the start command (`uvicorn app.api:app --host 0.0.0.0 --port $PORT`) and `/health`.
3. **+ New → Database → PostgreSQL**. On the web service, set `DATABASE_URL=${{Postgres.DATABASE_URL}}`.
4. Optional: `OPENAI_API_KEY` on the web service. Without it the board still works.
5. **Settings → Networking → Generate Domain**.
6. Seed from your laptop (local DB must already be loaded):

```bash
deploy/seed_remote.sh '<Postgres DATABASE_PUBLIC_URL from the Railway dashboard>'
```

Then restart the web service. Full notes, Render, and env vars: [DEPLOY.md](DEPLOY.md).

## Telling it as a story

Press **Prepare the story** once before you present. It captures every minute from 07:30 to 10:00 and writes up each alert as it opens, in order, waiting for each write-up so the snapshot at 08:25 carries the prose that was written at 08:25. About two minutes the first time; a rebuild reuses the written prose and takes a few seconds.

Then a slider appears with the morning's landmarks marked on it. Drag it, click a dot, or use the left and right arrow keys to step between landmarks. Every minute shows exactly what the manager would have seen: the board, the alerts, the feed, and anything you have already sent. Clicking an option while scrubbing records the decision at that minute of the story.

The landmarks are read off the event feed, not hand-placed:

| Time | Landmark |
|---|---|
| 07:40 | First cab fails to leave: Agent 20 |
| 08:01 | Technical Support alert opens |
| 08:25 | Agent 09 not collected |
| 08:47 | Agent 15 not collected |
| 08:49 | First arrival: Agent 22 |
| 09:00 | Shift starts |
| 09:05 | Grace period ends |
| 09:19 | Billing Support back to strength |
| 09:55 | Technical Support back to strength |

This is precomputed, and that is not a trick. Every snapshot is the same deterministic function the live clock runs, evaluated at that minute, with the same guard against reading the future. The narratives are real model output produced in order. **Back to live** returns to the running clock at any time.

## The demo, in six beats

Press **Run the morning** and watch, or press **Jump to 08:55** to open on the worst of it. Or prepare the story and step through the landmarks above.

1. **07:45.** Board green. 24 rostered, nobody due yet.
2. **08:12.** The cab for Agent 20 has not left its depot, 32 minutes late. First amber.
3. **08:25.** Billing Support alert opens at 92% projected coverage. The narrative arrives a few seconds later.
4. **08:47.** Agent 15 not collected, cab has passed. The alert now offers "Call the unaccounted riders".
5. **08:55.** Billing at 67%, service level 15%. Click **Move the early shift onto the queue**. The draft appears under Sent, naming four people who are verifiably on the floor.
6. **09:19.** Billing back to 83%. The alert resolves itself. Ask the chat: *"Who covered this morning, and how much overtime did we avoid?"*

## How it is built

Green is arithmetic. Purple is the model. The manager only sees the board on the left.

```mermaid
flowchart LR
  classDef ui fill:#dbeafe,stroke:#1e40af,color:#1e3a5f
  classDef api fill:#93c5fd,stroke:#1e3a5f,color:#1e3a5f
  classDef det fill:#a7f3d0,stroke:#047857,color:#064e3b
  classDef llm fill:#ddd6fe,stroke:#6d28d9,color:#4c1d95
  classDef data fill:#fed7aa,stroke:#c2410c,color:#7c2d12

  UI["Shift board<br/>queues · alerts · chat · story"]:::ui
  API["FastAPI :8000"]:::api

  subgraph DET["Deterministic core — Python, ~15ms"]
    CLK["Replay clock"]:::det
    MATH["Who is late · how short<br/>service level · what to do"]:::det
  end

  subgraph LLM["LLM — only when needed"]
    NAR["Narrator · 2 tools"]:::llm
    AST["Chat · 7 tools"]:::llm
  end

  PG[("Postgres<br/>trips · roster · alerts")]:::data

  UI -->|watch / act / ask| API
  API --> CLK
  CLK --> MATH
  PG --> CLK
  MATH -->|save| PG
  MATH -->|situation changed| NAR
  API -->|question| AST
  NAR -->|summary + drafts| API
  AST -->|answer| API
```

For a slide: [SVG](docs/architecture.svg) · [PNG](docs/architecture.png) · [Excalidraw](docs/architecture.excalidraw)

The model writes only when an alert opens, resolves, changes cause, changes recommendation, or crosses a severity band. A 150-tick morning across two queues produces about 11 narratives. Everything else is arithmetic.

One tick, without the model:

```mermaid
flowchart LR
  classDef det fill:#a7f3d0,stroke:#047857,color:#064e3b
  CLK[Replay clock]:::det --> ST[Rider state]:::det
  ST --> ETA[Arrival]:::det
  ETA --> Q[Queue headcount]:::det
  Q --> SLA[Erlang C + day]:::det
  SLA --> AL[Alert + options]:::det
  CTX[Benchmarks]:::det --> AL
  CAND[Cover candidates]:::det --> AL
```

### The pieces

| Path | What it does |
|---|---|
| `db/schema.sql` | Tables, views and indexes. Views carry the history maths. |
| `db/load.py` | CSV to Postgres via COPY, with every data quirk handled and counted. |
| `db/seed_roster.py` | Picks real riders for the team and cover pool; asserts a synthetic night shift. |
| `app/core/state.py` | Nine-state rider machine. Only reads what `now` reveals. |
| `app/core/eta.py` | Arrival projection with an honest uncertainty band. |
| `app/core/queue.py` | Headcount per queue, impact as a range, service level, day rollup. |
| `app/core/sla.py` | Erlang C, speed of answer, abandonment, adherence, agents needed. |
| `app/core/context.py` | Five benchmarks: team norm, weekday, recent, peer sites, structural slack. |
| `app/core/remediation.py` | Cover candidates verified on the floor; hold-over priced. |
| `app/core/alerts.py` | Triggers, cause, lifecycle with hysteresis, options scored on the contract. |
| `app/replay.py` | The clock. |
| `app/timeline.py` | The morning captured tick by tick, with landmarks derived from the feed. |
| `app/api.py` | Fourteen endpoints, all scoped by tenant, site, date and shift. |
| `agent/` | Tools, two agents, runner, metered usage. |
| `web/index.html` | The board. One file, no build step. |

## What is real and what is synthetic

| Thing | Source |
|---|---|
| Riders, trips, pickup and drop times, no-shows, vendors, cabs | Real. From the dataset. |
| The 24-person team and the 24-person cover pool | Real riders, chosen because they rode the shift 40+ days. |
| Which queue a rider serves, handle time, call forecast, service target | Synthetic. In `roster` and `queues`. |
| The night shift and positional handover | Synthetic, and flagged as such in the roster. Clearwater's trip log has no outbound legs near 09:00, so there was no night shift to read. We assert one because a 24/7 desk is what makes a late arrival cost something beyond a thin queue. |
| Overtime rate, cab-home time, cover fairness counter | Synthetic. In `app/config.py`. |

## Findings worth knowing

Things the data said that changed the design.

**Transport and the floor disagree about what a delay is.** 70% of arrivals that miss the shift start on this route are stamped `NODELAY` by the transport system. By the operator's measure nothing went wrong. The floor is short regardless. The alert senses the floor and never reads that column.

**The plan is inside its own error bar.** The median buffer between planned drop and shift start is 5 minutes. Journey-time noise is about 13 minutes. Half of arrivals are late by construction, and no amount of daily escalation fixes a schedule built that way. This is surfaced as a benchmark.

**Clearwater is the worst site on this shift.** 46% late against 29% across four peer sites. Cedar Ridge runs the same shift at under 1%.

**Staffing is not linear.** Losing four of twelve agents on a properly loaded queue takes service level from 92% to 15%, not to two-thirds. A headcount alone cannot convey that, which is why the service level is computed and shown.

**A twenty-minute gap does not break the day.** The interval collapses; the daily number holds at 92%. The alert says so, and escalates to operations only when the day is genuinely at risk. Telling a manager when not to escalate is worth as much as telling them when to.

## Honest limitations

- **No GPS.** The dataset has pickup and drop times only. Once a rider is aboard, nothing is observable until they arrive, so the projection carries about 13 minutes of irreducible spread. Impact is reported as a range for this reason, and the hard alert trigger is the deterministic "planned drop passed, no arrival", which has no false positives.
- **The projection is calibrated on ordinary days** and reads optimistic on bad ones. This is asserted in a test so nobody mistakes it for a bug.
- **Erlang C assumes** Poisson arrivals, exponential handle times, no abandonment, and interchangeable agents within a queue. Real workforce tools relax all four. The shape of the answer is what the decision depends on.
- **The night shift is invented.** Everything about it is labelled synthetic.

## Data quirks handled

All from the dataset's own README, each counted in the load log.

- `trip_id`, `stwid`, epochs and `delay_minutes` are comma-formatted strings in some files and clean numbers in others. Normalised on load.
- Four different date formats across five files. Parsed per file.
- Negative distances in `emp_data`. Nulled, 48 of them.
- A literal `"False"` in `alerts_data.severity`. Nulled, 15,037 of them.
- Dtype drift across the three monthly ride files. Reconciled on concat.
- `stwid = 0` is a placeholder. Filtered by the views.
- `Non Shift` and `Adhoc` shift labels. Filtered by the views.
- Two cover riders take a second inbound leg on the demo day. A view picks the one nearest their rostered start.
- Nulls are states, not errors: a null pickup and drop together mean the rider never boarded.

## Deployability

Postgres for everything durable, including the agent's sessions. Every table and every endpoint is keyed by `business_unit` and `office`, so pointing this at another tenant is a config change. The reasoning core does no model calls and no per-tick database round trips, which is what makes it credible at enterprise volume. The model is a one-line swap (`BESSEMER_MODEL`). Fallback drafts still send if the model is down.

This build is **production-shaped, not a finished product**. Keep: tenant-scoped API, Postgres, `/health`, secrets in env (never git). Wrap with MoveInSync's login and swap `app/replay.py` for their live trip feed. Do not quote the night-shift hold-over as measured fact — it is labelled synthetic. Honest list: [docs/SHIP.md](docs/SHIP.md).

Application code is MIT. The organiser dataset is not in this repo and is not ours to redistribute.
