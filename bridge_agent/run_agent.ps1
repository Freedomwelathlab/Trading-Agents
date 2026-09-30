<#
.SYNOPSIS
  Start the Trading OS bridge agent (Phase 104, D124).

.DESCRIPTION
  Creates bridge_agent\.venv on first run, installs requirements.txt into it,
  then runs `python -m bridge_agent` from this folder's parent so the package
  imports resolve. Configuration comes from bridge_agent\.env (copy
  .env.example). Pass -Check to run one heartbeat and exit (0 = every gateway
  reachable and logged in). Pass -Moomoo to also install futu-api.

  Restarts the agent if it exits unexpectedly, with a 30 s pause, until the
  window is closed or Ctrl+C is pressed.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\bridge_agent\run_agent.ps1 -Check
  powershell -ExecutionPolicy Bypass -File .\bridge_agent\run_agent.ps1
#>
param(
    [switch]$Check,
    [switch]$Moomoo,
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$parent = Split-Path -Parent $here
$venv = Join-Path $here ".venv"
$venvPython = Join-Path $venv "Scripts\python.exe"

if (-not (Test-Path (Join-Path $here ".env"))) {
    Write-Host "No bridge_agent\.env found. Copy bridge_agent\.env.example to bridge_agent\.env and fill it in." -ForegroundColor Yellow
    exit 2
}

if (-not (Test-Path $venvPython)) {
    Write-Host "Creating $venv ..."
    & $Python -m venv $venv
    if (-not $?) { Write-Host "Could not create a virtualenv with '$Python'." -ForegroundColor Red; exit 2 }
    & $venvPython -m pip install --quiet --upgrade pip
    & $venvPython -m pip install --quiet -r (Join-Path $here "requirements.txt")
}
if ($Moomoo) {
    & $venvPython -m pip install --quiet "futu-api>=9.0"
}

Set-Location $parent

if ($Check) {
    & $venvPython -m bridge_agent --check
    exit $LASTEXITCODE
}

while ($true) {
    & $venvPython -m bridge_agent
    $code = $LASTEXITCODE
    if ($code -eq 0 -or $code -eq 2) {
        # 0 = stopped on purpose, 2 = configuration error (restarting will not fix it)
        exit $code
    }
    Write-Host "bridge agent exited with code $code; restarting in 30 s (Ctrl+C to stop)" -ForegroundColor Yellow
    Start-Sleep -Seconds 30
}
