param(
  [string]$Campaign = "m8-distributed-10-day"
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$reportDir = Join-Path $root "reports\distributed_simulation"
New-Item -ItemType Directory -Force -Path $reportDir | Out-Null
if (-not $env:POSTGRES_PASSWORD -or -not $env:POSTGRES_TEST_PASSWORD) {
  throw "POSTGRES_PASSWORD and POSTGRES_TEST_PASSWORD must be set in the operator environment"
}
Push-Location $root
try {
  # Invoking this script is explicit operator approval to start/restart only the
  # project-scoped Docker services. It never changes Docker Desktop or Windows.
  & scripts\start_production_like.ps1
  if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

  docker compose up -d db_test
  if ($LASTEXITCODE -ne 0) { throw "Isolated PostgreSQL test database failed to start" }
  $env:TEST_POSTGRES_URL = "postgresql+psycopg://dse_test:$env:POSTGRES_TEST_PASSWORD@127.0.0.1:15432/dse_autotrader_test"
  $env:TEST_REDIS_URL = "redis://127.0.0.1:6379/15"
  Push-Location backend
  try {
    .\.venv\Scripts\python.exe -m pytest tests\test_milestone7_integrations.py -m integration -q
    if ($LASTEXITCODE -ne 0) { throw "PostgreSQL/Redis integration suite failed" }
  } finally {
    Pop-Location
  }

  docker compose exec -T backend python -m app.simulation_process --campaign $Campaign --start-day 1 --end-day 5 --total-days 10 --require-distributed
  if ($LASTEXITCODE -ne 0) { throw "Distributed phase 1 failed" }
  docker compose exec -T backend python -c "from app.core.database import SessionLocal; from app.services.task_queue import create_broker,enqueue_task; db=SessionLocal(); item=enqueue_task(db,create_broker(),'m8_corrupt_payload',{'corrupt':True},'m8-dead-letter',max_attempts=1); print(item.id); db.close()"
  if ($LASTEXITCODE -ne 0) { throw "Dead-letter injection failed" }

  docker compose kill worker scheduler
  docker compose --profile production-like up -d worker scheduler
  docker compose restart redis db
  docker compose --profile production-like up -d backend worker worker_2 scheduler
  docker compose exec -T backend python -m app.simulation_process --campaign $Campaign --start-day 6 --end-day 10 --total-days 10 --require-distributed
  if ($LASTEXITCODE -ne 0) { throw "Distributed phase 2 failed" }
  docker compose exec -T backend python -c "from sqlalchemy import select; from app.core.database import SessionLocal; from app.models import TaskRecord; from app.brokers.paper import PaperBroker; db=SessionLocal(); task=db.scalar(select(TaskRecord).where(TaskRecord.idempotency_key=='m8-dead-letter')); print({'dead_letter':task.state if task else None,'reconciliation':PaperBroker(db).reconcile()}); db.close(); raise SystemExit(0 if task and task.state=='dead_letter' else 2)"
  if ($LASTEXITCODE -ne 0) { throw "Dead-letter/reconciliation verification failed" }
  & scripts\status_production_like.ps1 | Set-Content -Encoding utf8 (Join-Path $reportDir "m8-distributed-final-status.json")
  Write-Output "Real PostgreSQL/Redis 10-day accelerated infrastructure validation passed. This is not real-market evidence."
} finally {
  Pop-Location
}
