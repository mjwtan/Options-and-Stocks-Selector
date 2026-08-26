<#
Runs the S15.5 dead-man's-switch check. Deliberately the simplest script
in this whole scheduling setup - a heartbeat checker that can itself fail
silently defeats the point.
#>
param(
    [Parameter(Mandatory = $true)][string]$ProjectDir,
    [Parameter(Mandatory = $true)][string]$PythonPath
)

Set-Location $ProjectDir

$logDir = Join-Path $ProjectDir "logs\scheduled"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$logFile = Join-Path $logDir "heartbeat_$(Get-Date -Format 'yyyy-MM-dd').log"

"=== $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') : check_heartbeat.py ===" | Out-File -Append -Encoding utf8 $logFile
& $PythonPath "scheduling\check_heartbeat.py" 2>&1 | Out-File -Append -Encoding utf8 $logFile
