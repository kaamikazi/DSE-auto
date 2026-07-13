$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$reportDir = Join-Path $root "reports\infrastructure"
New-Item -ItemType Directory -Force -Path $reportDir | Out-Null
if (-not $env:POSTGRES_PASSWORD) { throw "POSTGRES_PASSWORD must be set in the operator environment" }
$databaseName = "dse_m8_migration_$(Get-Date -Format yyyyMMdd_HHmmss)"
$destination = "postgresql+psycopg://dse:$($env:POSTGRES_PASSWORD)@127.0.0.1:5432/$databaseName"
Push-Location $root
try {
  docker compose exec -T db createdb -U dse $databaseName
  if ($LASTEXITCODE -ne 0) { throw "Failed to create isolated migration database" }
  $env:DATABASE_URL = $destination
  Push-Location backend
  try {
    .\.venv\Scripts\python.exe -m alembic upgrade head
    if ($LASTEXITCODE -ne 0) { throw "Alembic clean install failed" }
  } finally {
    Pop-Location
  }
  $result = & backend\.venv\Scripts\python.exe scripts\migrate_sqlite_to_postgresql.py `
    --source "sqlite:///backend/data/dse_autotrader.db" --destination $destination --execute
  if ($LASTEXITCODE -ne 0) { throw "SQLite-to-PostgreSQL copy or deterministic verification failed" }
  $result | Set-Content -Encoding utf8 (Join-Path $reportDir "$databaseName.json")
  $result
  Write-Output "Original SQLite remained read-only. Isolated PostgreSQL database: $databaseName"
} finally {
  Pop-Location
}
