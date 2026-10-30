# TrendBot — install/uninstall automatic backend operation (Windows, Task Scheduler).
#
# What this does:
#   install   -> registers a scheduled task "TrendBot-Backend" that:
#                 * starts automatically at user logon (and every 5 minutes if missing)
#                 * relaunches the backend if it crashes (restart loop in run_backend.ps1)
#                 * logs to data\backend.log / data\backend.err.log
#                 * shuts down cleanly on logoff (task stop -> wrapper exits)
#   uninstall -> removes the task and stops the backend.
#
# Run from an elevated PowerShell if you want SYSTEM-level start (optional):
#   powershell -ExecutionPolicy Bypass -File tools\install_autostart.ps1 install
# Non-elevated is fine for logon-triggered operation.

param([Parameter(Position=0)][string]$Action = "install")

$ErrorActionPreference = "Stop"
$root    = Split-Path -Parent (Split-Path -Leaf $PSScriptRoot) ; $root = (Resolve-Path "$PSScriptRoot\..").Path
$task    = "TrendBot-Backend"
$wrapper = Join-Path $PSScriptRoot "run_backend.ps1"
$dataDir = Join-Path $root "data"
if (-not (Test-Path $dataDir)) { New-Item -ItemType Directory -Path $dataDir | Out-Null }

function Get-PyCmd {
    foreach ($c in @("py", "python")) {
        $p = Get-Command $c -ErrorAction SilentlyContinue
        if ($p) { return $c }
    }
    throw "No 'py' or 'python' found on PATH."
}

if ($Action -eq "install") {
    $py = Get-PyCmd
    $argLine = "-NoProfile -ExecutionPolicy Bypass -File `"$wrapper`" -Python `"$py`""
    $action  = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $argLine -WorkingDirectory $root
    # Start at logon + keepalive check every 5 min (settings restart-on-failure handled inside wrapper too)
    $triggers = @(
        (New-ScheduledTaskTrigger -AtLogOn),
        (New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 5) -RepetitionDuration ([TimeSpan]::MaxValue))
    )
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
                -MultipleInstances IgnoreNew -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
                -ExecutionTimeLimit ([TimeSpan]::Zero)
    Unregister-ScheduledTask -TaskName $task -Confirm:$false -ErrorAction SilentlyContinue
    Register-ScheduledTask -TaskName $task -Action $action -Trigger $triggers -Settings $settings `
        -Description "TrendBot backend auto-start/restart (dashboard at http://127.0.0.1:8000)" | Out-Null
    Start-ScheduledTask -TaskName $task
    Write-Host "Installed + started scheduled task '$task'. Logs: $dataDir\backend.log"
    Write-Host "NOTE: MT5 terminal + EA must still be running for actual trading."
} elseif ($Action -eq "uninstall") {
    Stop-ScheduledTask -TaskName $task -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $task -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "Removed scheduled task '$task'."
} elseif ($Action -eq "status") {
    Get-ScheduledTask -TaskName $task -ErrorAction SilentlyContinue | Select-Object TaskName, State
    try { (Invoke-WebRequest -UseBasicParsing "http://127.0.0.1:8000/health" -TimeoutSec 3).Content } catch { Write-Host "backend not reachable" }
} else {
    Write-Host "Usage: install_autostart.ps1 [install|uninstall|status]"
}
