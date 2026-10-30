# TrendBot backend supervisor: keeps run_server.py alive, logs output, exits cleanly on Ctrl+C/stop.
# Invoked by install_autostart.ps1 (Task Scheduler). Manual dev use: py backend\run_server.py
param([string]$Python = "py")

$ErrorActionPreference = "Continue"
$root = (Resolve-Path "$PSScriptRoot\..").Path
Set-Location $root
$log    = Join-Path $root "data\backend.log"
$errlog = Join-Path $root "data\backend.err.log"
New-Item -ItemType Directory -Path (Join-Path $root "data") -Force | Out-Null

while ($true) {
    Add-Content $log "$(Get-Date -Format s) starting backend..."
    # .env (if present) is loaded by backend/envfile.py inside run_server.py
    & $Python "backend\run_server.py" 1>> $log 2>> $errlog
    $code = $LASTEXITCODE
    Add-Content $log "$(Get-Date -Format s) backend exited code=$code; restarting in 5s (Ctrl+C the task to stop)"
    Start-Sleep -Seconds 5
}
