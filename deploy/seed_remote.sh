#!/usr/bin/env bash
#
# Push the slice the demo actually reads into a hosted Postgres.
#
# The loaded database is 2.3M rows / 548 MB. That neither fits Render's 1 GB
# free tier nor moves over a home uplink in a sensible time. The board only
# ever queries one office, and the deepest lookback (app/core/context.py) is a
# few same-weekdays back, so one office's three months is the entire demo:
# ~114k trips, ~270k rider legs, ~9k trip alerts.
#
# Load the full dataset locally first (see README), then:
#
#   deploy/seed_remote.sh '<External Database URL from the Render dashboard>'
#
# Re-running is safe; it truncates the slice tables and refills them.

set -euo pipefail

REMOTE="${1:-${TARGET_DATABASE_URL:-}}"
if [ -z "$REMOTE" ]; then
    echo "usage: $0 '<postgres-url>'" >&2
    echo "  Render dashboard -> bessemer-db -> Connections -> External Database URL" >&2
    exit 1
fi

LOCAL="${BESSEMER_LOCAL_DSN:-postgresql://postgres@127.0.0.1:5432/bessemer}"
OFFICE="${BESSEMER_OFFICE:-Clearwater Campus}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# Single-quote escaping, because the office name is interpolated into SQL.
O="${OFFICE//\'/\'\'}"

echo "==> schema"
psql "$REMOTE" -v ON_ERROR_STOP=1 -q -f "$ROOT/db/schema.sql"

echo "==> clearing existing slice"
psql "$REMOTE" -v ON_ERROR_STOP=1 -q \
    -c "TRUNCATE roster, queues, trip_alerts, rider_legs, trips RESTART IDENTITY CASCADE"

# Streams local -> remote without ever landing a dump file on disk.
slice() {
    printf '    %-12s ' "$1"
    psql "$LOCAL" -v ON_ERROR_STOP=1 -q -c "\copy ($2) TO STDOUT WITH (FORMAT csv)" \
        | psql "$REMOTE" -v ON_ERROR_STOP=1 -q -c "\copy $1 FROM STDIN WITH (FORMAT csv)"
    psql "$REMOTE" -tAc "SELECT count(*) FROM $1"
}

echo "==> copying rows"
# queues before roster: roster.queue carries a foreign key into it.
slice queues      "SELECT * FROM queues WHERE office = '$O'"
slice roster      "SELECT * FROM roster WHERE office = '$O'"
slice trips       "SELECT * FROM trips  WHERE office = '$O'"
slice rider_legs  "SELECT * FROM rider_legs WHERE office = '$O'"
slice trip_alerts "SELECT a.* FROM trip_alerts a
                   JOIN trips t ON t.trip_id = a.trip_id
                   WHERE t.office = '$O'"

# rider_legs.id came across verbatim, so move the sequence past it.
psql "$REMOTE" -v ON_ERROR_STOP=1 -q \
    -c "SELECT setval('rider_legs_id_seq', COALESCE((SELECT max(id) FROM rider_legs), 1))"

echo "==> done. Redeploy or restart the web service to drop stale pooled connections."
