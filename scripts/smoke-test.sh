#!/usr/bin/env bash
# Brings up the Docker Compose stack and checks that it works end to end:
# the API takes pings, a failure takes a check down, and the scheduler and
# worker are running sweeps. CI runs this on every push.
set -euo pipefail

BASE=${TOCSIN_URL:-http://localhost:8000}
json() { python3 -c "import json,sys; print(json.load(sys.stdin)$1)"; }

docker compose up -d --build --wait --wait-timeout 180
trap 'docker compose logs --tail 50; docker compose down -v' EXIT

curl -fsS "$BASE/healthz"; echo

KEY=$(docker compose exec -T api tocsin keys create smoke-test 2>/dev/null)
AUTH=(-H "Authorization: Bearer $KEY")

CHECK=$(curl -fsS "${AUTH[@]}" -H 'Content-Type: application/json' \
  -d '{"name": "smoke test", "schedule": "@hourly"}' "$BASE/api/checks")
ID=$(echo "$CHECK" | json '["id"]')
PING=$(echo "$CHECK" | json '["ping_url"]' | sed "s#^http://localhost:8000#$BASE#")

curl -fsS "$PING" > /dev/null
test "$(curl -fsS "${AUTH[@]}" "$BASE/api/checks/$ID" | json '["state"]')" = up
curl -fsS --data-binary 'disk full' "$PING/1" > /dev/null
test "$(curl -fsS "${AUTH[@]}" "$BASE/api/checks/$ID" | json '["state"]')" = down
echo "pings: ok"

# The scheduler queues a sweep every five seconds and a worker runs it; the
# API reports when one last finished.
for _ in $(seq 30); do
  if curl -fsS "$BASE/metrics" | grep -q '^tocsin_last_sweep_timestamp_seconds'; then
    echo "scheduler and worker: ok"
    exit 0
  fi
  sleep 2
done
echo "no sweep finished within a minute" >&2
exit 1
