# Hosting the Shift Readiness Agent

One FastAPI process serves both the API and the board, so hosting is one web
service plus one Postgres. No separate frontend deploy, no build step.

```
browser ──HTTPS──> Render web service ──> Render Postgres  (trips, roster, alerts)
                   (uvicorn / FastAPI)  └─> OpenAI via LiteLLM  (narration, chat)
                   GET  /        -> web/index.html
                   GET  /board   -> the deterministic readiness board
                   GET  /health  -> liveness, no database needed
```

`GET /` hands the browser `web/index.html`. That page then calls `/board`,
`/alerts`, and `/timeline` on the same origin, so there is no CORS hop and no
API host to configure in the frontend. The API reads Postgres for every board
figure. Only narration and chat call the model, which is why a deploy with no
`OPENAI_API_KEY` still shows a fully working board.

## Deploy

`render.yaml` is a Render Blueprint that creates both resources and wires the
database credentials into the web service automatically.

1. Go to <https://dashboard.render.com/blueprints> and click **New Blueprint
   Instance**.
2. Point it at `github.com/peekuh/bessemer`, branch `main`. Render reads
   `render.yaml` and offers to create `bessemer` (web) and `bessemer-db`
   (Postgres).
3. It prompts for `OPENAI_API_KEY`. Paste one, or leave it blank and add it
   later under the service's **Environment** tab.
4. Apply. First build takes a few minutes. The app comes up at
   `https://bessemer.onrender.com` (Render appends a suffix if the name is
   taken; the dashboard shows the real URL).

The board will be empty until you seed the database.

## Seed the database

The organiser dataset is 2.3M rows and 548 MB loaded, which does not fit the
free tier. The board only ever queries one office, so the seed copies one
office in full: **114,174 trips, 270,279 rider legs, 8,577 trip alerts, 103 MB**.
Note it slices by office, not by date, so every same-weekday lookback the
board does still has its full three months of history behind it.

Load the full dataset locally first (see README), then stream the slice up:

```bash
deploy/seed_remote.sh '<External Database URL from the Render dashboard>'
```

Find that URL under **bessemer-db -> Connections -> External Database URL**.
The script applies `db/schema.sql`, truncates, and refills, so it is safe to
re-run. It takes about 9 seconds against a local target and a few minutes over
a home uplink. Restart the web service afterwards to drop pooled connections
that were opened against the empty schema.

To host a different site, set `BESSEMER_OFFICE` for both the seed script and
the web service, and reseed.

## Environment variables

| Variable | Set by | Needed for |
| --- | --- | --- |
| `PGHOST` `PGPORT` `PGUSER` `PGPASSWORD` `PGDATABASE` | Blueprint, from `bessemer-db` | everything |
| `OPENAI_API_KEY` | you, in the dashboard | narration and chat only |
| `BESSEMER_MODEL` | optional, defaults to `openai/gpt-5.6-luna` | picking another model |
| `BESSEMER_OFFICE` `BESSEMER_BU` `BESSEMER_SHIFT` `BESSEMER_DEMO_DATE` | optional | pointing at another tenant |

Every knob in `app/config.py` is env-overridable; the table lists the ones that
matter for a deploy.

## Redeploying

Push to `main` and Render rebuilds and restarts on its own. To change infra
(plans, new env vars), edit `render.yaml`, push, then **Manual Deploy ->
Apply Blueprint** so Render picks up the resource changes. Application code
changes need no Blueprint step. Data only changes when you re-run the seed.

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
