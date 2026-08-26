<#
Runs the two daily jobs (system-spec.md S9.2) in sequence:
  1. daily_monitor.py  - alert-only checks (S8.3/S9.2/S9.3), never submits orders
  2. track_performance.py - updates history/performance_summary.csv (S14)

Called by the "OptionsSelector-DailyMonitor" scheduled task registered by
register_tasks.ps1. All output is appended to logs/scheduled/daily_<date>.log -
this is just a "did it run" trail; the actual results live in
logs/monitor_*.json and history/performance_summary.csv.
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

"=== $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') : daily_monitor.py ===" | Out-File -Append $logFile
& $PythonPath "daily_monitor.py" @optArgs *>> $logFile

"=== $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') : track_performance.py ===" | Out-File -Append $logFile
& $PythonPath "track_performance.py" *>> $logFile
