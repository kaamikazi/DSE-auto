param(
  [Parameter(Mandatory = $true)][string]$BackupPath,
  [string]$TargetDatabase = "dse_autotrader_restore_verify"
)
$ErrorActionPreference = "Stop"
if (-not (Test-Path -LiteralPath $BackupPath)) { throw "Backup file not found" }
docker compose exec -T db dropdb --if-exists --username=dse $TargetDatabase
docker compose exec -T db createdb --username=dse $TargetDatabase
Get-Content -AsByteStream -Raw -LiteralPath $BackupPath |
  docker compose exec -T db pg_restore --exit-on-error --no-owner --username=dse --dbname=$TargetDatabase
docker compose exec -T db psql --username=dse --dbname=$TargetDatabase --command="SELECT count(*) AS tables FROM pg_tables WHERE schemaname='public';"
