#!/usr/bin/env bash
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
APP="$(dirname "$ROOT")/robot-shop"
[ -d "$APP" ] || git clone https://github.com/instana/robot-shop.git "$APP"
cd "$APP"
docker compose -p robot-shop pull
docker compose -p robot-shop up -d
docker compose -p robot-shop ps
echo "Store front: http://localhost:8080"
