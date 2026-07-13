$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$logDir = Join-Path $root "logs\production-like"
$stateDir = Join-Path $root "data\process-state"
New-Item -ItemType Directory -Force -Path $logDir, $stateDir | Out-Null
Push-Location $root
try {
  & backend\.venv\Scripts\python.exe scripts\operator.py infrastructure doctor --output-dir (Join-Path $root "reports\infrastructure")
  $doctor = Get-Content -LiteralPath "reports\infrastructure\infrastructure_doctor.json" -Raw | ConvertFrom-Json
  $blocking = $doctor.checks | Where-Object {
    -not $_.passed -and $_.name -notin @("postgresql_connectivity", "redis_connectivity")
  }
  if ($blocking) { throw "Infrastructure doctor has blocking failures. Review reports\infrastructure\infrastructure_doctor.md" }
  docker compose up -d db redis
  if ($LASTEXITCODE -ne 0) { throw "Database/Redis startup failed" }
  docker compose --profile production-like up -d backend scheduler worker worker_2 frontend
  if ($LASTEXITCODE -ne 0) { throw "Application process startup failed" }
  docker compose --profile production-like ps --format json | Set-Content -Encoding utf8 (Join-Path $stateDir "compose-status.json")
  docker compose --profile production-like logs --no-color --tail 200 | Set-Content -Encoding utf8 (Join-Path $logDir "startup.log")
  Write-Output "Production-like PAPER services started. LIVE TRADING remains disabled."
} finally {
  Pop-Location
}
