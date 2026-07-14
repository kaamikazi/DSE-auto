param(
  [Parameter(Mandatory=$true)]
  [ValidateSet('api_restarted','scheduler_killed','redis_restarted_with_queue')]
  [string]$Exercise,
  [Parameter(Mandatory=$true)]
  [ValidatePattern('^[a-zA-Z0-9_]+$')]
  [string]$ValidationDatabase,
  [string]$OutputDirectory = 'reports\incidents'
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$output = Join-Path $root $OutputDirectory
New-Item -ItemType Directory -Force -Path $output | Out-Null
$stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$evidencePath = Join-Path $output "${Exercise}_evidence_$stamp.json"
$reportPath = Join-Path $output "${Exercise}_real_$stamp.json"

function Get-RuntimeSnapshot {
  $memory = Get-CimInstance Win32_PerfFormattedData_PerfOS_Memory
  $services = @(docker compose --profile production-like ps --format json | ConvertFrom-Json |
    Select-Object Service,State,Health,ID)
  return [ordered]@{
    captured_at = (Get-Date).ToUniversalTime().ToString('o')
    available_gib = [math]::Round($memory.AvailableBytes / 1GB, 3)
    commit_headroom_gib = [math]::Round(($memory.CommitLimit - $memory.CommittedBytes) / 1GB, 3)
    hard_paging_per_second = [double]$memory.PageReadsPersec + [double]$memory.PageWritesPersec
    services = $services
  }
}

function Wait-Service([string]$Service, [bool]$RequireHealth) {
  $deadline = (Get-Date).AddSeconds(120)
  do {
    $item = @(docker compose --profile production-like ps --format json $Service | ConvertFrom-Json) |
      Select-Object -First 1
    if ($item.State -eq 'running' -and (-not $RequireHealth -or $item.Health -eq 'healthy')) {
      return
    }
    Start-Sleep -Seconds 2
  } while ((Get-Date) -lt $deadline)
  throw "$Service did not recover"
}

Push-Location $root
try {
  $passwordLine = Get-Content .env | Where-Object { $_ -like 'POSTGRES_PASSWORD=*' } |
    Select-Object -First 1
  if (-not $passwordLine) { throw 'POSTGRES_PASSWORD is not configured' }
  $password = $passwordLine.Substring('POSTGRES_PASSWORD='.Length)
  $env:DATABASE_URL = "postgresql+psycopg://dse:$password@127.0.0.1:5432/$ValidationDatabase"
  $before = Get-RuntimeSnapshot
  @{before=$before; approved=$true; paper_only=$true} | ConvertTo-Json -Depth 8 |
    Set-Content -LiteralPath $evidencePath -Encoding utf8
  $opened = backend\.venv\Scripts\python.exe scripts\real_outage_incident.py open --exercise $Exercise --evidence $evidencePath | ConvertFrom-Json

  $taskEvidence = $null
  if ($Exercise -eq 'api_restarted') {
    docker compose stop -t 30 backend
    if ($LASTEXITCODE -ne 0) { throw 'API stop failed' }
    $during = Get-RuntimeSnapshot
    docker compose start backend
    if ($LASTEXITCODE -ne 0) { throw 'API restart failed' }
    Wait-Service 'backend' $true
    Invoke-RestMethod http://127.0.0.1:8000/api/v1/health | Out-Null
  } elseif ($Exercise -eq 'scheduler_killed') {
    docker compose kill scheduler
    if ($LASTEXITCODE -ne 0) { throw 'Scheduler kill failed' }
    $during = Get-RuntimeSnapshot
    docker compose start scheduler
    if ($LASTEXITCODE -ne 0) { throw 'Scheduler restart failed' }
    Wait-Service 'scheduler' $false
  } else {
    docker compose stop -t 30 worker
    if ($LASTEXITCODE -ne 0) { throw 'Worker stop failed before queueing' }
    $taskKey = "m9-redis-restart-$([guid]::NewGuid().ToString('N'))"
    docker compose exec -T backend python -c "from app.core.database import SessionLocal; from app.services.task_queue import create_broker,enqueue_task; db=SessionLocal(); t=enqueue_task(db,create_broker(),'simulation_day',{'day':2,'symbols':['GP'],'strategies':['buy_hold@1']},'$taskKey'); print(t.id); db.close()" | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Could not queue the restart-safe task' }
    docker compose stop -t 30 redis
    if ($LASTEXITCODE -ne 0) { throw 'Redis stop failed' }
    $during = Get-RuntimeSnapshot
    docker compose start redis
    if ($LASTEXITCODE -ne 0) { throw 'Redis restart failed' }
    Wait-Service 'redis' $true
    docker compose start worker
    if ($LASTEXITCODE -ne 0) { throw 'Worker restart failed' }
    Wait-Service 'worker' $false
    $deadline = (Get-Date).AddSeconds(45)
    do {
      $taskState = (docker compose exec -T backend python -c "from sqlalchemy import select; from app.core.database import SessionLocal; from app.models import TaskRecord; db=SessionLocal(); t=db.scalar(select(TaskRecord).where(TaskRecord.idempotency_key=='$taskKey')); print(f'{t.state}|{t.attempts}|{t.lease_owner}|{t.lease_expires_at}' if t else 'missing'); db.close()").Trim()
      if ($taskState -like 'succeeded*') { break }
      Start-Sleep -Seconds 2
    } while ((Get-Date) -lt $deadline)
    if ($taskState -notlike 'succeeded|1|None|None') { throw "Queued task recovery failed: $taskState" }
    $taskEvidence = @{idempotency_key=$taskKey; final_state=$taskState; exactly_once=$true}
  }

  $resolved = backend\.venv\Scripts\python.exe scripts\real_outage_incident.py resolve --incident-id $opened.incident_id | ConvertFrom-Json
  $after = Get-RuntimeSnapshot
  $memorySafe = $after.available_gib -ge 1.5 -and $after.commit_headroom_gib -ge 8
  $report = [ordered]@{
    exercise = $Exercise
    execution_mode = 'real_serialized_substage_verification'
    incident_id = $opened.incident_id
    before = $before
    during = $during
    after = $after
    resolved = $resolved
    task_evidence = $taskEvidence
    memory_safe_after = $memorySafe
    passed = [bool]($resolved.resolved -and $resolved.audit_valid -and $resolved.reconciliation.healthy -and $memorySafe)
    safety = @{trading_mode='paper';live_trading_enabled=$false;broker_adapter='disabled'}
  }
  $report | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $reportPath -Encoding utf8
  $report | ConvertTo-Json -Depth 10
  if (-not $report.passed) { exit 2 }
} finally {
  Remove-Item Env:DATABASE_URL -ErrorAction SilentlyContinue
  Pop-Location
}
