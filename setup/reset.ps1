param(
    [int]$TimeoutSeconds = $(if ($env:ROBOT_SHOP_START_TIMEOUT) { [int]$env:ROBOT_SHOP_START_TIMEOUT } else { 180 })
)
$ErrorActionPreference = 'Stop'

function Fail([string]$Message) {
    throw "FAIL: $Message"
}

$root = Split-Path -Parent $PSScriptRoot
$app = Join-Path (Split-Path -Parent $root) 'robot-shop'
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { Fail 'Docker CLI was not found. Start/install Docker Desktop or Docker Engine.' }
if ($TimeoutSeconds -le 0) { Fail 'TimeoutSeconds must be a positive number of seconds.' }
if (-not (Get-Command python -ErrorAction SilentlyContinue)) { Fail 'Python was not found. Install Python and project requirements.' }
& docker info *> $null
if ($LASTEXITCODE -ne 0) { Fail 'Docker daemon is unavailable. Start Docker and try again.' }
& docker compose version *> $null
if ($LASTEXITCODE -ne 0) { Fail "Docker Compose v2 is unavailable (expected 'docker compose')." }
if (-not (Test-Path -LiteralPath $app)) { Fail "Robot Shop checkout is missing at $app. Run setup/start.ps1 first." }
if (-not (Test-Path -LiteralPath (Join-Path $app 'docker-compose.yaml'))) { Fail "Missing $(Join-Path $app 'docker-compose.yaml')." }

$composeArgs = @('compose', '-p', 'robot-shop', '-f', 'docker-compose.yaml')
Push-Location $app
try {
    & docker @composeArgs down -v
    if ($LASTEXITCODE -ne 0) { Fail 'Could not stop and clear Robot Shop containers and volumes.' }
    & docker @composeArgs up -d
    if ($LASTEXITCODE -ne 0) { Fail 'Robot Shop restart failed.' }

    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    $allRunning = $false
    $ids = @()
    while ([DateTime]::UtcNow -lt $deadline) {
        $ids = @(& docker @composeArgs ps -q | Where-Object { $_ })
        if ($ids.Count -gt 0) {
            $allRunning = $true
            foreach ($id in $ids) {
                $state = (& docker inspect -f '{{.State.Status}}' $id 2>$null | Out-String).Trim()
                if ($LASTEXITCODE -ne 0 -or $state -ne 'running') { $allRunning = $false; break }
            }
            if ($allRunning) { break }
        }
        Start-Sleep -Seconds 3
    }
    if (-not $allRunning -or $ids.Count -eq 0) {
        & docker @composeArgs ps
        Fail "Robot Shop containers did not all reach running state within ${TimeoutSeconds}s."
    }
    Write-Host 'PASS: Robot Shop containers restarted and are running.'
    if ($env:ROBOT_SHOP_URL) { Write-Host "Checking storefront at $env:ROBOT_SHOP_URL." }
    else { Write-Host 'Storefront URL: http://localhost:8080 (set ROBOT_SHOP_URL to override).' }
    & python (Join-Path $root 'setup/health.py')
    if ($LASTEXITCODE -ne 0) { Fail 'Robot Shop health checks failed.' }
}
finally {
    Pop-Location
}
