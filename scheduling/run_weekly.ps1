<#
Runs the weekly sizing job (system-spec.md S9.1) - compute-only, by
explicit choice: this does NOT call trade_from_csv.py. It writes
target_positions.csv and archives the week to history/; you review it and
submit the actual trades yourself.

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

$optArgs = @()
if ($OptionsEnabled) { $optArgs += "--options-enabled" }

"=== $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') : position_sizing.py ===" | Out-File -Append $logFile
& $PythonPath "position_sizing.py" $CsvPath "--output" "target_positions.csv" @optArgs *>> $logFile
