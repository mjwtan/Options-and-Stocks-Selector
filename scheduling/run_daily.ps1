<#
Runs the daily monitoring job (system-spec.md S9.2) - alert-only checks
(S8.3/S9.2/S9.3), never submits orders. Scheduled Tue-Fri, not Monday -
Monday morning is covered by the weekly job instead (S15.2's job split).

Called by the "OptionsSelector-DailyMonitor" scheduled task registered by
register_tasks.ps1. Output is appended to logs/scheduled/daily_<date>.log -
just a "did it run" trail; the actual results live in logs/monitor_*.json.
#>
param(
    [Parameter(Mandatory = $true)][string]$ProjectDir,
    [Parameter(Mandatory = $true)][string]$PythonPath,
    [switch]$OptionsEnabled
)

Set-Location $ProjectDir

$logDir = Join-Path $ProjectDir "logs\scheduled"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$logFile = Join-Path $logDir "daily_$(Get-Date -Format 'yyyy-MM-dd').log"

$optArgs = @()
if ($OptionsEnabled) { $optArgs += "--options-enabled" }

"=== $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') : daily_monitor.py ===" | Out-File -Append -Encoding utf8 $logFile
& $PythonPath "daily_monitor.py" @optArgs 2>&1 | Out-File -Append -Encoding utf8 $logFile
