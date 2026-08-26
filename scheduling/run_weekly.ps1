<#
Runs the weekly sizing job (system-spec.md S9.1) - compute-only, by
explicit choice: this does NOT call trade_from_csv.py. It writes
target_positions.csv and archives the week to history/; you review it and
submit the actual trades yourself.

Registered against a Mon-Fri trigger (not just Monday) so a holiday
Monday doesn't silently skip the week - is_weekly_run_day.py gates the
actual work to whichever day is genuinely the week's first NYSE session
(S15.2). Every other day this fires, it logs and exits without touching
position_sizing.py at all.

If $CsvPath hasn't been refreshed since last week (you paste the weekly
prompt into an LLM to regenerate it - see mdinstructions/weekly-stock-
prompt.md), position_sizing.py's own staleness guard fails this run
closed rather than re-processing a stale file - that's expected, not a
bug in this script.
#>
param(
    [Parameter(Mandatory = $true)][string]$ProjectDir,
    [Parameter(Mandatory = $true)][string]$PythonPath,
    [string]$CsvPath = "top20.csv",
    [switch]$OptionsEnabled
)

Set-Location $ProjectDir

$logDir = Join-Path $ProjectDir "logs\scheduled"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$logFile = Join-Path $logDir "weekly_$(Get-Date -Format 'yyyy-MM-dd').log"

& $PythonPath "scheduling\is_weekly_run_day.py" 2>&1 | Out-File -Append -Encoding utf8 $logFile
if ($LASTEXITCODE -ne 0) {
    exit 0  # not the week's first trading day - not a failure, just not today
}

$optArgs = @()
if ($OptionsEnabled) { $optArgs += "--options-enabled" }

"=== $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') : position_sizing.py ===" | Out-File -Append -Encoding utf8 $logFile
& $PythonPath "position_sizing.py" $CsvPath "--output" "target_positions.csv" @optArgs 2>&1 | Out-File -Append -Encoding utf8 $logFile
