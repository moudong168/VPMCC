$ErrorActionPreference = "Stop"
chcp 65001 | Out-Null
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
$env:PYTHONUTF8 = "1"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = "C:\Users\ddouer\AppData\Local\Programs\Python\Python310\python.exe"
Set-Location $ScriptDir
Write-Host ""
Write-Host "========== PMCC - OPEND mode =========="
Write-Host "Workspace: $ScriptDir"
Write-Host "Python: $Python"
Write-Host "Requires: Futu OpenD running + futu-api installed"
Write-Host ""
Write-Host "Schwab positions:"
Write-Host "- Export thinkorswim CSV if changed, or press Enter to reuse saved positions."
Write-Host ""
$SchwabCsv = Read-Host "Schwab CSV path (Enter if unchanged)"
$MsftIvRank = Read-Host "US.MSFT IV Rank override (Enter to skip)"
$MsftIvPct = Read-Host "US.MSFT IV Percentile override (Enter to skip)"
$NvdaIvRank = Read-Host "US.NVDA IV Rank override (Enter to skip)"
$NvdaIvPct = Read-Host "US.NVDA IV Percentile override (Enter to skip)"
$Args = @("futu_option_decision.py", "--pmcc-opend")
if ($SchwabCsv) { $Args += @("--schwab-import-positions", $SchwabCsv) }
$ivr = @(); if ($MsftIvRank) { $ivr += "US.MSFT=$MsftIvRank" }; if ($NvdaIvRank) { $ivr += "US.NVDA=$NvdaIvRank" }
if ($ivr.Count -gt 0) { $Args += @("--iv-rank-overrides", ($ivr -join ",")) }
$ivp = @(); if ($MsftIvPct) { $ivp += "US.MSFT=$MsftIvPct" }; if ($NvdaIvPct) { $ivp += "US.NVDA=$NvdaIvPct" }
if ($ivp.Count -gt 0) { $Args += @("--iv-percentile-overrides", ($ivp -join ",")) }
& $Python @Args
Write-Host ""; Write-Host "Done. Press Enter to close."; Read-Host | Out-Null
