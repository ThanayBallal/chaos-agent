#!/usr/bin/env bash
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$(dirname "$ROOT")/robot-shop"
docker compose -p robot-shop down -v
docker compose -p robot-shop up -d
docker compose -p robot-shop ps
