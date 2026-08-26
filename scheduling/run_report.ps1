<#
Runs the report job (system-spec.md S15.1/S15.2) - Friday after close.
Updates history/performance_summary.csv (S14: actual vs. equal-weight vs.
SPY). Structurally incapable of submitting an order: track_performance.py
only ever imports AlpacaMarketData (read-only quotes/bars), never
AlpacaEquityBroker or AlpacaOptionsBroker - it doesn't have a way to place
an order even if something in it were wrong.
#>
param(
    [Parameter(Mandatory = $true)][string]$ProjectDir,
    [Parameter(Mandatory = $true)][string]$PythonPath
)

Set-Location $ProjectDir

$logDir = Join-Path $ProjectDir "logs\scheduled"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$logFile = Join-Path $logDir "report_$(Get-Date -Format 'yyyy-MM-dd').log"

"=== $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') : track_performance.py ===" | Out-File -Append -Encoding utf8 $logFile
& $PythonPath "track_performance.py" 2>&1 | Out-File -Append -Encoding utf8 $logFile
