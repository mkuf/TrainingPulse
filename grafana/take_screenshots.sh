#!/usr/bin/env bash
# Capture full-page screenshots of the bundled Grafana dashboards by running
# the Playwright Python container against the live stack.
#
# Defaults assume the stack is running locally (docker compose up -d) and that
# the database has activities (e.g. seed_demo_data.py). Output PNGs land in
# ./screenshots/ at the repo root.
#
# Environment overrides:
#   PLAYWRIGHT_IMAGE  default: mcr.microsoft.com/playwright/python:v1.49.1-jammy
#   COMPOSE_NETWORK   default: strava-sync_default (compose project name + _default)
#   GRAFANA_HOST      default: grafana (the in-network compose service name)
#   OUT_DIR           default: <repo>/screenshots
#   WIDTH             default: 1920
#   HEIGHT            default: 1080
#   WAIT_MS           default: 15000
#   TIMERANGE         default: from=now-30d&to=now (fitness + account_overview)
#   ACTIVITY_ID       default: highest-TRIMP ride in the last 30 days
#
# The Playwright *Python* image bundles Chromium but not the `playwright` PyPI
# package, so the container run installs that wheel once per invocation (fast).
#
# The activity-detail dashboard uses a time range computed from the chosen
# activity's start_date and moving_time so streams render at full resolution.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

PLAYWRIGHT_IMAGE="${PLAYWRIGHT_IMAGE:-mcr.microsoft.com/playwright/python:v1.49.1-jammy}"
COMPOSE_NETWORK="${COMPOSE_NETWORK:-strava-sync_default}"
GRAFANA_HOST="${GRAFANA_HOST:-grafana}"
OUT_DIR="${OUT_DIR:-$REPO_ROOT/screenshots}"
WIDTH="${WIDTH:-1920}"
HEIGHT="${HEIGHT:-1080}"
WAIT_MS="${WAIT_MS:-15000}"
TIMERANGE="${TIMERANGE:-from=now-30d&to=now}"

if ! docker compose -f "$REPO_ROOT/docker-compose.yml" ps --status running --services 2>/dev/null | grep -q '^db$'; then
  echo "error: the 'db' service is not running. Start the stack first: docker compose up -d" >&2
  exit 1
fi

mkdir -p "$OUT_DIR"

ACTIVITY_ID="${ACTIVITY_ID:-}"
if [ -z "$ACTIVITY_ID" ]; then
  ACTIVITY_ID=$(
    docker compose -f "$REPO_ROOT/docker-compose.yml" exec -T db \
      psql -U trainingpulse -d trainingpulse -t -A -c \
      "SELECT id FROM activities WHERE sport_type IN ('Ride','VirtualRide','MountainBikeRide') AND best_20min_power IS NOT NULL AND has_heartrate AND start_date > now() - interval '30 days' ORDER BY trimp DESC NULLS LAST LIMIT 1;"
  )
fi
if [ -z "$ACTIVITY_ID" ]; then
  echo "error: could not find a recent activity for activity-detail screenshot. Seed the DB first (docker compose exec app python seed_demo_data.py --force)." >&2
  exit 1
fi

ACTIVITY_RANGE=$(
  docker compose -f "$REPO_ROOT/docker-compose.yml" exec -T db \
    psql -U trainingpulse -d trainingpulse -t -A -F'|' -c \
    "SELECT EXTRACT(EPOCH FROM (start_date - interval '5 minutes'))::bigint * 1000, EXTRACT(EPOCH FROM (start_date + (moving_time + 300) * interval '1 second'))::bigint * 1000 FROM activities WHERE id = $ACTIVITY_ID;"
)
ACTIVITY_FROM_MS="${ACTIVITY_RANGE%%|*}"
ACTIVITY_TO_MS="${ACTIVITY_RANGE##*|}"

echo "Using activity_id=$ACTIVITY_ID range [$ACTIVITY_FROM_MS, $ACTIVITY_TO_MS]"
echo "Spawning $PLAYWRIGHT_IMAGE on network $COMPOSE_NETWORK ..."

docker run --rm \
  --network "$COMPOSE_NETWORK" \
  -v "$OUT_DIR:/out" \
  -v "$SCRIPT_DIR/screenshot_dashboards.py:/work/screenshot_dashboards.py:ro" \
  -w /work \
  -e GRAFANA_URL="http://$GRAFANA_HOST:3000" \
  -e OUT_DIR=/out \
  -e WIDTH="$WIDTH" \
  -e HEIGHT="$HEIGHT" \
  -e WAIT_MS="$WAIT_MS" \
  -e TIMERANGE="$TIMERANGE" \
  -e ACTIVITY_ID="$ACTIVITY_ID" \
  -e ACTIVITY_FROM_MS="$ACTIVITY_FROM_MS" \
  -e ACTIVITY_TO_MS="$ACTIVITY_TO_MS" \
  --entrypoint /bin/bash \
  "$PLAYWRIGHT_IMAGE" \
  -lc "pip install --quiet --disable-pip-version-check --root-user-action=ignore 'playwright>=1.49.1,<1.50' && python3 screenshot_dashboards.py"

echo "Done. Screenshots in $OUT_DIR"
ls -la "$OUT_DIR"
