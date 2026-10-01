$ErrorActionPreference = "Stop"
chcp 65001 | Out-Null
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
$env:PYTHONUTF8 = "1"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = "python"
Set-Location $ScriptDir
Write-Host ""
Write-Host "========== PMCC - CBOE + saved positions =========="
Write-Host "Skips Futu OpenD connection."
Write-Host "Reads saved positions from pmcc_futu_positions.json + pmcc_schwab_positions.json."
Write-Host "Option data from CBOE + Yahoo (no futu-api needed)."
Write-Host ""
$SchwabCsv = Read-Host "Schwab CSV path to replace saved positions (Enter to reuse)"
$MsftIvRank = Read-Host "US.MSFT IV Rank override (Enter to skip)"
$MsftIvPct = Read-Host "US.MSFT IV Percentile override (Enter to skip)"
$NvdaIvRank = Read-Host "US.NVDA IV Rank override (Enter to skip)"
$NvdaIvPct = Read-Host "US.NVDA IV Percentile override (Enter to skip)"
$Args = @("futu_option_decision.py", "--cboe", "--pmcc-opend")
if ($SchwabCsv) { $Args += @("--schwab-import-positions", $SchwabCsv) }
$ivr = @(); if ($MsftIvRank) { $ivr += "US.MSFT=$MsftIvRank" }; if ($NvdaIvRank) { $ivr += "US.NVDA=$NvdaIvRank" }
if ($ivr.Count -gt 0) { $Args += @("--iv-rank-overrides", ($ivr -join ",")) }
$ivp = @(); if ($MsftIvPct) { $ivp += "US.MSFT=$MsftIvPct" }; if ($NvdaIvPct) { $ivp += "US.NVDA=$NvdaIvPct" }
if ($ivp.Count -gt 0) { $Args += @("--iv-percentile-overrides", ($ivp -join ",")) }
& $Python @Args
Write-Host ""; Write-Host "Done. Press Enter to close."; Read-Host | Out-Null
