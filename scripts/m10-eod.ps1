param(
  [Parameter(Mandatory = $true)][string]$CampaignId,
  [Parameter(Mandatory = $true)][string]$MarketDate
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Push-Location $root
try {
  Get-Content .env | ForEach-Object {
    if ($_ -match '^\s*([^#][A-Za-z_][A-Za-z0-9_]*)=(.*)$') {
      [Environment]::SetEnvironmentVariable($matches[1], $matches[2], 'Process')
    }
  }
  if ($env:TRADING_MODE -ne 'paper' -or $env:LIVE_TRADING_ENABLED -ne 'false' -or
      $env:BROKER_ADAPTER -ne 'disabled') {
    throw 'Paper-only safety configuration failed'
  }
  $env:POSTGRES_VALIDATION_DATABASE = 'dse_autotrader'
  $stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
  $output = Join-Path $root 'reports\recovery'
  New-Item -ItemType Directory -Force -Path $output | Out-Null
  $backup = Join-Path $output "m10_eod_${MarketDate}_$stamp.dump"
  & "$PSScriptRoot\postgres_backup.ps1" -OutputPath $backup -Database dse_autotrader
  if ($LASTEXITCODE -ne 0) { throw 'EOD PostgreSQL backup failed' }
  $restoreDatabase = "m10_eod_restore_$($stamp.Replace('_', ''))"
  & "$PSScriptRoot\postgres_restore.ps1" -BackupPath $backup -TargetDatabase $restoreDatabase
  if ($LASTEXITCODE -ne 0) { throw 'EOD isolated restore failed' }
  docker compose exec -T db dropdb --if-exists --username=dse $restoreDatabase
  if ($LASTEXITCODE -ne 0) { throw 'EOD isolated restore cleanup failed' }
  $evidencePath = Join-Path $output "m10_eod_backup_$stamp.json"
  [ordered]@{
    successful = $true
    restore_verified = $true
    path = $backup
    sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $backup).Hash
    bytes = (Get-Item -LiteralPath $backup).Length
    verified_at = (Get-Date).ToUniversalTime().ToString('o')
  } | ConvertTo-Json | Set-Content -LiteralPath $evidencePath -Encoding utf8
  & backend\.venv\Scripts\python.exe scripts\real_market_operator.py eod-run $CampaignId `
    --market-date $MarketDate --backup-evidence $evidencePath
  if ($LASTEXITCODE -ne 0) { throw 'EOD evidence workflow failed closed' }
} finally {
  Pop-Location
}
