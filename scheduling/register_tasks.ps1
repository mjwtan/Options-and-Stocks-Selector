<#
Registers the two system-spec.md S9 scheduled jobs in Windows Task
Scheduler, per what was chosen when this was built:
  - Daily job: alert-only (daily_monitor.py) + performance tracking
    (track_performance.py). Never submits an order.
  - Weekly job: position_sizing.py only (compute + archive). Does NOT run
    trade_from_csv.py - you review target_positions.csv and submit trades
    yourself.

Run once to set up. Re-run to update (uses -Force). Both tasks run under
your own user account at the times below - edit -DailyTime/-WeeklyTime/
-WeeklyDay as you like, or just edit the registered task afterward in the
Task Scheduler GUI.

Usage:
    .\scheduling\register_tasks.ps1                    # equity-only
    .\scheduling\register_tasks.ps1 -OptionsEnabled    # also check/size options

To inspect:
    Get-ScheduledTask -TaskName "OptionsSelector-*" | Get-ScheduledTaskInfo

To remove both:
    Get-ScheduledTask -TaskName "OptionsSelector-*" | Unregister-ScheduledTask -Confirm:$false
#>
param(
    [string]$PythonPath = (Get-Command python).Source,
    [string]$DailyTime = "08:00",
    [string]$WeeklyTime = "07:30",
    [string]$WeeklyDay = "Monday",
    [string]$CsvPath = "top20.csv",
    [switch]$OptionsEnabled
)

$ErrorActionPreference = "Stop"
$ProjectDir = Split-Path -Parent $PSScriptRoot

$optSwitch = if ($OptionsEnabled) { "-OptionsEnabled" } else { "" }

# --- Daily: monitoring + performance tracking (S9.2/S9.3), Mon-Fri -----------
$dailyScript = Join-Path $PSScriptRoot "run_daily.ps1"
$dailyArgList = "-NoProfile -ExecutionPolicy Bypass -File `"$dailyScript`" -ProjectDir `"$ProjectDir`" -PythonPath `"$PythonPath`" $optSwitch"
$dailyAction = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $dailyArgList -WorkingDirectory $ProjectDir
$dailyTrigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At $DailyTime
Register-ScheduledTask -TaskName "OptionsSelector-DailyMonitor" -Action $dailyAction -Trigger $dailyTrigger `
    -Description "system-spec.md S9.2/S9.3 daily monitoring + S14 performance tracking. Alert-only - submits no orders." `
    -Force | Out-Null
Write-Output "Registered OptionsSelector-DailyMonitor: weekdays at $DailyTime"

# --- Weekly: sizing only (S9.1), compute-only, no order submission ----------
$weeklyScript = Join-Path $PSScriptRoot "run_weekly.ps1"
$weeklyArgList = "-NoProfile -ExecutionPolicy Bypass -File `"$weeklyScript`" -ProjectDir `"$ProjectDir`" -PythonPath `"$PythonPath`" -CsvPath `"$CsvPath`" $optSwitch"
$weeklyAction = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $weeklyArgList -WorkingDirectory $ProjectDir
$weeklyTrigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $WeeklyDay -At $WeeklyTime
Register-ScheduledTask -TaskName "OptionsSelector-WeeklySizing" -Action $weeklyAction -Trigger $weeklyTrigger `
    -Description "system-spec.md S9.1 weekly sizing - computes target_positions.csv and archives to history/. Does NOT submit trades; refresh $CsvPath before this runs or it fails closed on the staleness guard." `
    -Force | Out-Null
Write-Output "Registered OptionsSelector-WeeklySizing: ${WeeklyDay}s at $WeeklyTime"

Write-Output ""
Write-Output "Reminder: refresh $CsvPath (the weekly LLM prompt) before $WeeklyDay $WeeklyTime, or the weekly job will fail closed on the staleness guard - that's expected, not a bug."
