$ErrorActionPreference = "Stop"
chcp 65001 | Out-Null
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
$env:PYTHONUTF8 = "1"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = "C:\Users\ddouer\AppData\Local\Programs\Python\Python310\python.exe"
Set-Location $ScriptDir
Write-Host ""
Write-Host "========== PMCC - CSV 模式（无需 OpenD） =========="
Write-Host "Workspace: $ScriptDir"
Write-Host "Python: $Python"
Write-Host "Requires: 无需 Futu OpenD；行情走 CBOE 延迟数据（约15分钟）"
Write-Host ""
Write-Host "持仓来源："
Write-Host "- 把 Schwab / 富途最新持仓 CSV 放进下面的 CSV 目录后回车"
Write-Host "- 目录里没有新 CSV 时，自动沿用上次记录的持仓"
Write-Host ""
$DefaultCsvDir = Split-Path -Parent $ScriptDir
$CsvDir = Read-Host "CSV 目录（回车默认: $DefaultCsvDir）"
if (-not $CsvDir) { $CsvDir = $DefaultCsvDir }
$MsftIvRank = Read-Host "US.MSFT IV Rank override (Enter to skip)"
$MsftIvPct = Read-Host "US.MSFT IV Percentile override (Enter to skip)"
$NvdaIvRank = Read-Host "US.NVDA IV Rank override (Enter to skip)"
$NvdaIvPct = Read-Host "US.NVDA IV Percentile override (Enter to skip)"
$Args = @("futu_option_decision.py", "--csv-positions", "--csv-dir", $CsvDir)
$ivr = @(); if ($MsftIvRank) { $ivr += "US.MSFT=$MsftIvRank" }; if ($NvdaIvRank) { $ivr += "US.NVDA=$NvdaIvRank" }
if ($ivr.Count -gt 0) { $Args += @("--iv-rank-overrides", ($ivr -join ",")) }
$ivp = @(); if ($MsftIvPct) { $ivp += "US.MSFT=$MsftIvPct" }; if ($NvdaIvPct) { $ivp += "US.NVDA=$NvdaIvPct" }
if ($ivp.Count -gt 0) { $Args += @("--iv-percentile-overrides", ($ivp -join ",")) }
& $Python @Args
Write-Host ""; Write-Host "Done. Press Enter to close."; Read-Host | Out-Null
