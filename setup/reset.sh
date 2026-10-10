#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP="$(dirname "$ROOT")/robot-shop"
TIMEOUT_SECONDS="${ROBOT_SHOP_START_TIMEOUT:-180}"
PYTHON="${PYTHON:-python3}"
fail() { echo "FAIL: $*" >&2; exit 1; }

command -v docker >/dev/null 2>&1 || fail "Docker CLI was not found. Start/install Docker Desktop or Docker Engine."
[[ "$TIMEOUT_SECONDS" =~ ^[1-9][0-9]*$ ]] || fail "ROBOT_SHOP_START_TIMEOUT must be a positive number of seconds."
command -v "$PYTHON" >/dev/null 2>&1 || fail "Python executable '$PYTHON' was not found."
docker info >/dev/null 2>&1 || fail "Docker daemon is unavailable. Start Docker and try again."
docker compose version >/dev/null 2>&1 || fail "Docker Compose v2 is unavailable (expected 'docker compose')."
[[ -d "$APP" ]] || fail "Robot Shop checkout is missing at $APP. Run setup/start.sh first."
[[ -f "$APP/docker-compose.yaml" ]] || fail "Missing $APP/docker-compose.yaml."
compose=(docker compose -p robot-shop -f docker-compose.yaml)
cd "$APP"

"${compose[@]}" down -v || fail "Could not stop and clear Robot Shop containers and volumes."
"${compose[@]}" up -d || fail "Robot Shop restart failed."

deadline=$((SECONDS + TIMEOUT_SECONDS))
all_running=false
ids=()
while ((SECONDS < deadline)); do
  ids=()
  while IFS= read -r id; do
    [[ -n "$id" ]] && ids+=("$id")
  done < <("${compose[@]}" ps -q)
  if ((${#ids[@]} > 0)); then
    all_running=true
    for id in "${ids[@]}"; do
      state="$(docker inspect -f '{{.State.Status}}' "$id" 2>/dev/null || true)"
      if [[ "$state" != running ]]; then all_running=false; break; fi
    done
    if [[ "$all_running" == true ]]; then break; fi
  fi
  sleep 3
done
if ((${#ids[@]} == 0)) || [[ "$all_running" != true ]]; then
  "${compose[@]}" ps >&2 || true
  fail "Robot Shop containers did not all reach running state within ${TIMEOUT_SECONDS}s."
fi
echo "PASS: Robot Shop containers restarted and are running."
if ! "$PYTHON" "$ROOT/setup/health.py"; then fail "Robot Shop health checks failed."; fi
