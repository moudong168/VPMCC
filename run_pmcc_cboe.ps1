$ErrorActionPreference = "Stop"
chcp 65001 | Out-Null
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
$env:PYTHONUTF8 = "1"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = "python"
Set-Location $ScriptDir
Write-Host ""
Write-Host "========== PMCC - CBOE mode (no OpenD) =========="
Write-Host "Does NOT need Futu OpenD or futu-api."
Write-Host "Option chain + Greeks from CBOE free API (15 min delay)."
Write-Host "HV + trend from Yahoo Finance."
Write-Host ""
$Symbol = Read-Host "Ticker symbol (e.g. US.NVDA, US.MSFT)"
if (-not $Symbol) { $Symbol = "US.NVDA" }
$IvRank = Read-Host "IV Rank override (Enter to skip)"
$Iv = Read-Host "IV %% override (Enter to skip)"
$Hv = Read-Host "HV %% override (Enter to skip)"
$Trend = Read-Host "Trend override UP/DOWN/FLAT (Enter to skip)"
$Args = @("futu_option_decision.py", $Symbol, "--cboe")
if ($IvRank) { $Args += @("--iv-rank", $IvRank) }
if ($Iv) { $Args += @("--iv", $Iv) }
if ($Hv) { $Args += @("--hv", $Hv) }
if ($Trend) { $Args += @("--trend", $Trend) }
Write-Host ""; Write-Host "Running PMCC CBOE mode for $Symbol ..."; Write-Host ""
& $Python @Args
Write-Host ""; Write-Host "Done. Press Enter to close."; Read-Host | Out-Null
