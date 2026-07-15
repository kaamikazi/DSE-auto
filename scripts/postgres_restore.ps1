param(
  [Parameter(Mandatory = $true)][string]$BackupPath,
  [string]$TargetDatabase = "dse_autotrader_restore_verify"
)
$ErrorActionPreference = "Stop"
if (-not (Test-Path -LiteralPath $BackupPath)) { throw "Backup file not found" }
docker compose exec -T db dropdb --if-exists --username=dse $TargetDatabase
docker compose exec -T db createdb --username=dse $TargetDatabase
$containerBackup = "/tmp/dse-autotrader-restore-$([guid]::NewGuid().ToString('N')).dump"
try {
  docker compose cp $BackupPath "db:$containerBackup"
  if ($LASTEXITCODE -ne 0) { throw "Could not stage PostgreSQL backup in the db container" }
  docker compose exec -T db pg_restore --exit-on-error --no-owner --username=dse `
    --dbname=$TargetDatabase $containerBackup
  if ($LASTEXITCODE -ne 0) { throw "PostgreSQL restore failed" }
  docker compose exec -T db psql --username=dse --dbname=$TargetDatabase `
    --command="SELECT count(*) AS tables FROM pg_tables WHERE schemaname='public';"
  if ($LASTEXITCODE -ne 0) { throw "PostgreSQL restored-database verification failed" }
} finally {
  docker compose exec -T db rm -f $containerBackup | Out-Null
}
