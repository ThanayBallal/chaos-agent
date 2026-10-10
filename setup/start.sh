#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP="$(dirname "$ROOT")/robot-shop"
LOAD=false
TIMEOUT_SECONDS="${ROBOT_SHOP_START_TIMEOUT:-180}"
PYTHON="${PYTHON:-python3}"

usage() {
  echo "Usage: $0 [--load]" >&2
}

while (($#)); do
  case "$1" in
    --load) LOAD=true ;;
    -h|--help) usage; exit 0 ;;
    *) usage; echo "Unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

fail() { echo "FAIL: $*" >&2; exit 1; }

command -v docker >/dev/null 2>&1 || fail "Docker CLI was not found. Start/install Docker Desktop or Docker Engine."
[[ "$TIMEOUT_SECONDS" =~ ^[1-9][0-9]*$ ]] || fail "ROBOT_SHOP_START_TIMEOUT must be a positive number of seconds."
command -v "$PYTHON" >/dev/null 2>&1 || fail "Python executable '$PYTHON' was not found."
docker info >/dev/null 2>&1 || fail "Docker daemon is unavailable. Start Docker and try again."
docker compose version >/dev/null 2>&1 || fail "Docker Compose v2 is unavailable (expected 'docker compose')."

if [[ ! -d "$APP" ]]; then
  command -v git >/dev/null 2>&1 || fail "Robot Shop is missing at $APP and git is unavailable for cloning."
  git clone https://github.com/instana/robot-shop.git "$APP" || fail "Could not clone Robot Shop into $APP."
fi
[[ -f "$APP/docker-compose.yaml" ]] || fail "Missing $APP/docker-compose.yaml."
compose=(docker compose -p robot-shop -f docker-compose.yaml)
if [[ "$LOAD" == true ]]; then
  [[ -f "$APP/docker-compose-load.yaml" ]] || fail "Load requested, but $APP/docker-compose-load.yaml is missing."
  compose+=(-f docker-compose-load.yaml)
fi
cd "$APP"

"${compose[@]}" pull || fail "Could not pull Robot Shop images."
"${compose[@]}" up -d || fail "Robot Shop Compose startup failed."

deadline=$((SECONDS + TIMEOUT_SECONDS))
ids=()
all_running=false
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
if ((${#ids[@]} == 0)) || [[ "${all_running:-false}" != true ]]; then
  "${compose[@]}" ps >&2 || true
  fail "Robot Shop containers did not all reach running state within ${TIMEOUT_SECONDS}s."
fi

echo "PASS: all Robot Shop containers are running."
if [[ "${ROBOT_SHOP_URL:-http://localhost:8080}" != http://localhost:8080 ]]; then
  echo "Checking storefront at ${ROBOT_SHOP_URL}."
else
  echo "Storefront URL: ${ROBOT_SHOP_URL:-http://localhost:8080} (set ROBOT_SHOP_URL to override)."
fi
if [[ "$LOAD" == true ]]; then echo "Load generator service was included."; fi
if ! "$PYTHON" "$ROOT/setup/health.py"; then fail "Robot Shop health checks failed."; fi
