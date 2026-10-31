#!/bin/sh
# Starts a built runtime image as the app and the MCP server against the
# compose database, and checks that both answer. Catches what the tests cannot
# see: the base image, the installed packages' native parts, and startup.
#
#   .github/smoke/smoke-test.sh <image>
set -eu

cd "$(dirname "$0")/../.."
export SMOKE_IMAGE="$1"
compose="docker compose -p trainingpulse-smoke -f docker-compose.yml -f .github/smoke/compose.yml"

finish() {
    status=$?
    if [ "$status" -ne 0 ]; then
        $compose logs app mcp
    fi
    $compose down -v --remove-orphans >/dev/null 2>&1
    exit "$status"
}
trap finish EXIT

$compose up -d --no-build --wait db
$compose up -d --no-build app mcp
for service in app mcp; do
    $compose exec -T "$service" python - "$service" < .github/smoke/check.py
done
