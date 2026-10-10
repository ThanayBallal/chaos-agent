$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$app = Join-Path (Split-Path -Parent $root) "robot-shop"
Set-Location $app
docker compose -p robot-shop down -v
docker compose -p robot-shop up -d
docker compose -p robot-shop ps
