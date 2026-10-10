$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$app = Join-Path (Split-Path -Parent $root) "robot-shop"
if (-not (Test-Path $app)) { git clone https://github.com/instana/robot-shop.git $app }
Set-Location $app
docker compose -p robot-shop pull
docker compose -p robot-shop up -d
docker compose -p robot-shop ps
Write-Host "Store front: http://localhost:8080"
