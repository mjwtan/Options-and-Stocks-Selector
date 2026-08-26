<#
Registers the system-spec.md S9/S15 scheduled jobs in Windows Task
Scheduler, matching S15.1's three-entry-point split plus the S15.5
heartbeat check:
  - Weekly (Mon-Fri trigger, self-gated to the week's actual first trading
    day - S15.2): position_sizing.py only. Does NOT run trade_from_csv.py -
    you review target_positions.csv and submit trades yourself.
  - Daily (Tue-Fri): daily_monitor.py. Alert-only, never submits orders.
    Not Monday - Monday morning is the weekly job's job.
  - Report (Friday, after close): track_performance.py. S14's actual vs.
    equal-weight vs. SPY comparison.
  - Heartbeat (Mon-Fri): the S15.5 dead-man's-switch check.

Known limitation, stated plainly: Windows Task Scheduler triggers store a
fixed LOCAL wall-clock time and repeat at it indefinitely - there is no
native "always mean 08:15 America/New_York" trigger. This script converts
the spec's ET times to local time *as of the moment you run it*, which is
correct for most of the year but can drift by an hour during the ~1-2 week
windows around the UK/US DST transitions (their switch dates don't align).
Re-run this script near late March and late October to re-true it up.
system-spec.md S15.3 also flags local-machine scheduling itself as
"development only, not for anything that matters" - see the GitHub
Actions workflows under .github/workflows/ for the recommended path.

Run once to set up. Re-run to update (uses -Force).

Usage:
    .\scheduling\register_tasks.ps1                    # equity-only
    .\scheduling\register_tasks.ps1 -OptionsEnabled    # also check/size options

To inspect:
    Get-ScheduledTask -TaskName "OptionsSelector-*" | Get-ScheduledTaskInfo

To remove all four:
    Get-ScheduledTask -TaskName "OptionsSelector-*" | Unregister-ScheduledTask -Confirm:$false
#>
param(
    [string]$PythonPath = (Get-Command python).Source,
    [string]$WeeklyTimeET = "08:15",
    [string]$DailyTimeET = "08:15",
    [string]$ReportTimeET = "17:30",
    [string]$HeartbeatTimeET = "09:00",
    [string]$CsvPath = "top20.csv",
    [switch]$OptionsEnabled
)

$ErrorActionPreference = "Stop"
$ProjectDir = Split-Path -Parent $PSScriptRoot

function Convert-EasternTimeToLocal {
    param([string]$TimeOfDay)
    $easternZone = [System.TimeZoneInfo]::FindSystemTimeZoneById("Eastern Standard Time")
    $parts = $TimeOfDay -split ':'
    $today = Get-Date
    $easternDateTime = Get-Date -Year $today.Year -Month $today.Month -Day $today.Day -Hour ([int]$parts[0]) -Minute ([int]$parts[1]) -Second 0
    $unspecified = [DateTime]::SpecifyKind($easternDateTime, [DateTimeKind]::Unspecified)
    $utc = [System.TimeZoneInfo]::ConvertTimeToUtc($unspecified, $easternZone)
    return $utc.ToLocalTime().ToString("HH:mm")
}

$weeklyLocal = Convert-EasternTimeToLocal $WeeklyTimeET
$dailyLocal = Convert-EasternTimeToLocal $DailyTimeET
$reportLocal = Convert-EasternTimeToLocal $ReportTimeET
$heartbeatLocal = Convert-EasternTimeToLocal $HeartbeatTimeET

Write-Output "ET -> local (as of now): weekly $WeeklyTimeET->$weeklyLocal, daily $DailyTimeET->$dailyLocal, report $ReportTimeET->$reportLocal, heartbeat $HeartbeatTimeET->$heartbeatLocal"
Write-Output ""

$optSwitch = if ($OptionsEnabled) { "-OptionsEnabled" } else { "" }

# --- Weekly: S9.1/S15.1, Mon-Fri trigger self-gated to the first trading day ---
$weeklyScript = Join-Path $PSScriptRoot "run_weekly.ps1"
$weeklyArgList = "-NoProfile -ExecutionPolicy Bypass -File `"$weeklyScript`" -ProjectDir `"$ProjectDir`" -PythonPath `"$PythonPath`" -CsvPath `"$CsvPath`" $optSwitch"
$weeklyAction = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $weeklyArgList -WorkingDirectory $ProjectDir
$weeklyTrigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At $weeklyLocal
Register-ScheduledTask -TaskName "OptionsSelector-WeeklySizing" -Action $weeklyAction -Trigger $weeklyTrigger `
    -Description "system-spec.md S9.1 weekly sizing (self-gated to the week's first trading day, S15.2). Computes and archives target_positions.csv only - does not submit trades." `
    -Force | Out-Null
Write-Output "Registered OptionsSelector-WeeklySizing: weekdays at $weeklyLocal local ($WeeklyTimeET ET), self-gated to the week's first trading day"

# --- Daily: S9.2, Tue-Fri (Monday is the weekly job's) --------------------
$dailyScript = Join-Path $PSScriptRoot "run_daily.ps1"
$dailyArgList = "-NoProfile -ExecutionPolicy Bypass -File `"$dailyScript`" -ProjectDir `"$ProjectDir`" -PythonPath `"$PythonPath`" $optSwitch"
$dailyAction = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $dailyArgList -WorkingDirectory $ProjectDir
$dailyTrigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Tuesday, Wednesday, Thursday, Friday -At $dailyLocal
Register-ScheduledTask -TaskName "OptionsSelector-DailyMonitor" -Action $dailyAction -Trigger $dailyTrigger `
    -Description "system-spec.md S9.2/S9.3 daily monitoring (Tue-Fri; Monday is covered by the weekly job). Alert-only - submits no orders." `
    -Force | Out-Null
Write-Output "Registered OptionsSelector-DailyMonitor: Tue-Fri at $dailyLocal local ($DailyTimeET ET)"

# --- Report: S14/S15.2, Friday after close --------------------------------
$reportScript = Join-Path $PSScriptRoot "run_report.ps1"
$reportArgList = "-NoProfile -ExecutionPolicy Bypass -File `"$reportScript`" -ProjectDir `"$ProjectDir`" -PythonPath `"$PythonPath`""
$reportAction = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $reportArgList -WorkingDirectory $ProjectDir
$reportTrigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Friday -At $reportLocal
Register-ScheduledTask -TaskName "OptionsSelector-Report" -Action $reportAction -Trigger $reportTrigger `
    -Description "system-spec.md S14/S15.2 performance report, Friday after close. Read-only - never constructs a broker client." `
    -Force | Out-Null
Write-Output "Registered OptionsSelector-Report: Fridays at $reportLocal local ($ReportTimeET ET)"

# --- Heartbeat: S15.5 dead-man's-switch ------------------------------------
$heartbeatScript = Join-Path $PSScriptRoot "run_heartbeat.ps1"
$heartbeatArgList = "-NoProfile -ExecutionPolicy Bypass -File `"$heartbeatScript`" -ProjectDir `"$ProjectDir`" -PythonPath `"$PythonPath`""
$heartbeatAction = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $heartbeatArgList -WorkingDirectory $ProjectDir
$heartbeatTrigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At $heartbeatLocal
Register-ScheduledTask -TaskName "OptionsSelector-Heartbeat" -Action $heartbeatAction -Trigger $heartbeatTrigger `
    -Description "system-spec.md S15.5 dead-man's-switch - alerts if a job hasn't recorded a successful run recently." `
    -Force | Out-Null
Write-Output "Registered OptionsSelector-Heartbeat: weekdays at $heartbeatLocal local ($HeartbeatTimeET ET)"

Write-Output ""
Write-Output "Reminder: refresh $CsvPath (the weekly LLM prompt) before the weekly job's first eligible day, or it fails closed on the staleness guard - that's expected, not a bug."
