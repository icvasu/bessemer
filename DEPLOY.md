# Hosting the Shift Readiness Agent

One FastAPI process serves both the API and the board, so hosting is one web
service plus one Postgres. No separate frontend deploy, no build step.

```
browser ──HTTPS──> host (Railway / Render) ──> Postgres  (trips, roster, alerts)
                   (FastAPI)                  └─> OpenAI via LiteLLM  (narration, chat)
                   GET  /        -> web/index.html
                   GET  /board   -> the deterministic readiness board
                   GET  /health  -> liveness, no database needed
```

`GET /` hands the browser `web/index.html`. That page then calls `/board`,
`/alerts`, and `/timeline` on the same origin, so there is no CORS hop and no
API host to configure in the frontend. The API reads Postgres for every board
figure. Only narration and chat call the model, which is why a deploy with no
`OPENAI_API_KEY` still shows a fully working board.

The submission repo is <https://github.com/icvasu/bessemer>. Railway is the
host that matches this process. Render is the same shape. Vercel can run the
code but is a worse fit for the clock.

## Deploy on Railway

`railway.toml` sets the start command and `/health`. You still add Postgres
and seed it.

1. Go to <https://railway.com/new> and choose **Deploy from GitHub repo**.
2. Select `icvasu/bessemer`, branch `main`.
3. **+ New → Database → PostgreSQL**. On the web service, add
   `DATABASE_URL=${{Postgres.DATABASE_URL}}` (use the database service's
   actual name if it is not `Postgres`).
4. Optional: `OPENAI_API_KEY` on the web service. The board works without it.
5. **Settings → Networking → Generate Domain**.
6. Seed, then **Restart** the web service:

```bash
deploy/seed_remote.sh '<DATABASE_PUBLIC_URL from the Postgres service>'
```

Use the **public** URL. The seeder runs on your laptop; the internal
`.railway.internal` host is not reachable from home.

## Deploy on Render

`render.yaml` is a Render Blueprint that creates both resources and wires the
database credentials into the web service automatically.

1. Go to <https://dashboard.render.com/blueprints> and click **New Blueprint
   Instance**.
2. Point it at `github.com/icvasu/bessemer`, branch `main`. Render reads
   `render.yaml` and offers to create `bessemer` (web) and `bessemer-db`
   (Postgres).
3. It prompts for `OPENAI_API_KEY`. Paste one, or leave it blank and add it
   later under the service's **Environment** tab.
4. Apply. First build takes a few minutes. The app comes up at
   `https://bessemer.onrender.com` (Render appends a suffix if the name is
   taken; the dashboard shows the real URL).

The board will be empty until you seed the database.

## Deploy on Vercel

Vercel runs the same FastAPI app as one function. It does not include
Postgres, so the database is a Neon project (Vercel Marketplace) or any
reachable Postgres, including the Render one above.

1. Push this branch, then go to <https://vercel.com/new> and import
   `github.com/icvasu/bessemer`.
2. Vercel reads `pyproject.toml` (`tool.vercel.entrypoint = "app.api:app"`)
   and `vercel.json`. No build command to set.
3. Add a Postgres. Easiest: Vercel dashboard → **Storage → Create Database →
   Neon**. That injects `DATABASE_URL`, `DATABASE_URL_UNPOOLED`, and the `PG*`
   variables the app already reads.
4. Add `OPENAI_API_KEY` under **Settings → Environment Variables**
   (Production + Preview). Without it the board still works; only narration
   and chat fail.
5. Deploy. The app comes up at `https://<project>.vercel.app`.
6. Seed, then redeploy or hit `/health` so the next request opens a fresh
   pool against the filled schema.

```bash
# Neon dashboard → Connection string, or Vercel → Storage → the Neon URL
# Prefer the unpooled / direct URL for the seeder (COPY needs a real session).
deploy/seed_remote.sh '<DATABASE_URL_UNPOOLED>'
```

CLI equivalent, once `npx vercel` is logged in:

```bash
npx vercel --prod
npx vercel env add OPENAI_API_KEY
```

**What is different from Render.** The clock used to live only in a
background asyncio task. Vercel ends that task when the request ends, so
`/board` now catches the clock up from wall time on each read. Keep the tab
open so Fluid compute reuses the same instance; a cold start begins the
morning again. "Prepare the story" can take about two minutes and will
time out on the Hobby 60s cap — jump to 08:55 instead, or prepare locally.

## Seed the database

The organiser dataset is 2.3M rows and 548 MB loaded, which does not fit the
free tier. The board only ever queries one office, so the seed copies one
office in full: **114,174 trips, 270,279 rider legs, 8,577 trip alerts, 103 MB**.
Note it slices by office, not by date, so every same-weekday lookback the
board does still has its full three months of history behind it.

Load the full dataset locally first (see README), then stream the slice up:

```bash
deploy/seed_remote.sh '<postgres URL>'
```

Use the Render **External Database URL**, or Neon's **direct / unpooled**
connection string (COPY cannot run through the pooler). The script applies
`db/schema.sql`, truncates, and refills, so it is safe to re-run. It takes
about 9 seconds against a local target and a few minutes over a home uplink.
Restart or redeploy the web service afterwards to drop pooled connections
that were opened against the empty schema.

To host a different site, set `BESSEMER_OFFICE` for both the seed script and
the web service, and reseed.

## Environment variables

| Variable | Set by | Needed for |
| --- | --- | --- |
| `PGHOST` `PGPORT` `PGUSER` `PGPASSWORD` `PGDATABASE` | Render Blueprint | everything |
| `DATABASE_URL` / `DATABASE_URL_UNPOOLED` | Railway Postgres, or Neon on Vercel | same, used if `BESSEMER_DSN` is unset |
| `OPENAI_API_KEY` | you, in the dashboard | narration and chat only |
| `BESSEMER_MODEL` | optional, defaults to `openai/gpt-5.6-luna` | picking another model |
| `BESSEMER_OFFICE` `BESSEMER_BU` `BESSEMER_SHIFT` `BESSEMER_DEMO_DATE` | optional | pointing at another tenant |

Every knob in `app/config.py` is env-overridable; the table lists the ones that
matter for a deploy.

## Redeploying

Push to `main` and the connected host rebuilds on its own (Render, or Vercel
if the GitHub repo is linked). To change Render infra (plans, new env vars),
edit `render.yaml`, push, then **Manual Deploy -> Apply Blueprint**.
Application code changes need no Blueprint step. Data only changes when you
re-run the seed.

## Free tier limits

- The web service sleeps after 15 minutes idle; the next request pays roughly
  a 50 second cold start. Hit the URL a few minutes before a demo.
- Free Postgres expires 30 days after creation and is capped at 1 GB. The
  103 MB slice fits with room to spare, but the expiry is a hard date. For
  anything beyond the hackathon, move the database to a paid plan.
- Model calls are billed by OpenAI, not Render.

## Running locally

Unchanged, and still the fastest loop:

```bash
uv run uvicorn app.api:app --port 8000
open http://localhost:8000
```

Local defaults to Postgres on `127.0.0.1:5432/bessemer`. The only difference
in the hosted setup is that `PG*` point somewhere else.
