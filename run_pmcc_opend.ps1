$ErrorActionPreference = "Stop"

chcp 65001 | Out-Null
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
$env:PYTHONUTF8 = "1"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$PythonCommand = Get-Command python -ErrorAction SilentlyContinue
if (-not $PythonCommand) {
    $PythonCommand = Get-Command py -ErrorAction SilentlyContinue
}
if (-not $PythonCommand) {
    throw "Python was not found. Install Python or add it to PATH before running this script."
}
$Python = $PythonCommand.Source
$Program = Join-Path $ScriptDir "futu_option_decision.py"
$RuntimeAppData = Join-Path $ScriptDir ".runtime-appdata"

New-Item -ItemType Directory -Force -Path $RuntimeAppData | Out-Null
$env:APPDATA = (Resolve-Path $RuntimeAppData).Path

Write-Host ""
Write-Host "Important: Please start Futu OpenD first, log in, and keep it running."
Write-Host "If Futu OpenD is not running, the program cannot read positions or option data."
Write-Host ""
Write-Host "Step 1: Futu positions will be read automatically through Futu OpenD."
Write-Host "Step 2: Schwab positions come from a thinkorswim Position Statement CSV."
Write-Host "Step 3: If Schwab positions are unchanged, leave the CSV path blank; the saved Schwab record will be reused and shown for confirmation."
Write-Host "Optional cold-start IV source from Futu APP. Leave blank once pmcc_iv_history.json has enough history."
Write-Host ""

$SchwabCsv = Read-Host "Schwab thinkorswim Position Statement CSV path (press Enter if unchanged)"
$MsftFutuIvRank = Read-Host "US.MSFT Futu APP IV grade / IV Rank overrides"
$MsftFutuIvPercentile = Read-Host "US.MSFT Futu APP IV percentile overrides"
$NvdaFutuIvRank = Read-Host "US.NVDA Futu APP IV grade / IV Rank overrides"
$NvdaFutuIvPercentile = Read-Host "US.NVDA Futu APP IV percentile overrides"

$Arguments = @($Program, "--pmcc-opend")
if (-not [string]::IsNullOrWhiteSpace($SchwabCsv)) {
    $Arguments += @("--schwab-import-positions", $SchwabCsv)
}
$IvRankOverrides = @()
if (-not [string]::IsNullOrWhiteSpace($MsftFutuIvRank)) {
    $IvRankOverrides += "US.MSFT=$MsftFutuIvRank"
}
if (-not [string]::IsNullOrWhiteSpace($NvdaFutuIvRank)) {
    $IvRankOverrides += "US.NVDA=$NvdaFutuIvRank"
}
if ($IvRankOverrides.Count -gt 0) {
    $Arguments += @("--iv-rank-overrides", ($IvRankOverrides -join ","))
}

$IvPercentileOverrides = @()
if (-not [string]::IsNullOrWhiteSpace($MsftFutuIvPercentile)) {
    $IvPercentileOverrides += "US.MSFT=$MsftFutuIvPercentile"
}
if (-not [string]::IsNullOrWhiteSpace($NvdaFutuIvPercentile)) {
    $IvPercentileOverrides += "US.NVDA=$NvdaFutuIvPercentile"
}
if ($IvPercentileOverrides.Count -gt 0) {
    $Arguments += @("--iv-percentile-overrides", ($IvPercentileOverrides -join ","))
}

& $Python @Arguments
