param(
  [Parameter(Mandatory=$true)]
  [ValidatePattern('^[a-zA-Z0-9_]+$')]
  [string]$ValidationDatabase,
  [string]$OutputDirectory = 'reports\distributed_exercises'
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$output = Join-Path $root $OutputDirectory
New-Item -ItemType Directory -Force -Path $output | Out-Null

function Get-RuntimeSnapshot {
  $memory = Get-CimInstance Win32_PerfFormattedData_PerfOS_Memory
  $pagefile = Get-Counter -Counter '\Paging File(_Total)\% Usage' -MaxSamples 1
  $disk = Get-Counter -Counter @(
    '\PhysicalDisk(_Total)\Avg. Disk sec/Read',
    '\PhysicalDisk(_Total)\Avg. Disk Queue Length'
  ) -MaxSamples 1
  return [ordered]@{
    captured_at = (Get-Date).ToUniversalTime().ToString('o')
    available_gib = [math]::Round($memory.AvailableBytes / 1GB, 3)
    commit_headroom_gib = [math]::Round(($memory.CommitLimit - $memory.CommittedBytes) / 1GB, 3)
    pagefile_used_percent = [math]::Round($pagefile.CounterSamples[0].CookedValue, 2)
    disk_read_latency_ms = [math]::Round($disk.CounterSamples[0].CookedValue * 1000, 2)
    disk_queue_length = [math]::Round($disk.CounterSamples[1].CookedValue, 2)
    services = @(docker compose --profile production-like ps --format json | ConvertFrom-Json |
      Select-Object Service,State,Health,ID)
  }
}

function Invoke-Probe([string[]]$Arguments) {
  Push-Location (Join-Path $root 'backend')
  try {
    $payload = & .\.venv\Scripts\python.exe ..\scripts\real_distributed_probe.py @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Distributed probe failed: $($Arguments -join ' ')" }
    return $payload | ConvertFrom-Json
  } finally { Pop-Location }
}

function Wait-TaskState([string]$Key, [string]$State, [int]$TimeoutSeconds = 60) {
  $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
  do {
    $status = Invoke-Probe @('task-status','--idempotency-key',$Key)
    if ($status.task.state -eq $State) { return $status }
    Start-Sleep -Seconds 1
  } while ((Get-Date) -lt $deadline)
  throw "Task did not reach $State"
}

function Wait-Service([string]$Service, [bool]$RequireHealth, [int]$TimeoutSeconds = 120) {
  $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
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

function Wait-ContainerExit([string]$Name, [int]$TimeoutSeconds = 90) {
  $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
  do {
    $state = (& docker inspect $Name --format '{{.State.Status}}').Trim()
    if ($state -in @('exited','dead')) { return }
    Start-Sleep -Seconds 1
  } while ((Get-Date) -lt $deadline)
  throw "Verification container did not exit: $Name"
}

Push-Location $root
try {
  $passwordLine = Get-Content .env | Where-Object { $_ -like 'POSTGRES_PASSWORD=*' } |
    Select-Object -First 1
  if (-not $passwordLine) { throw 'POSTGRES_PASSWORD is not configured' }
  $password = $passwordLine.Substring('POSTGRES_PASSWORD='.Length)
  $env:DATABASE_URL = "postgresql+psycopg://dse:$password@127.0.0.1:5432/$ValidationDatabase"
  $env:REDIS_URL = 'redis://127.0.0.1:6379/0'
  $stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
  $correlation = [guid]::NewGuid().ToString()
  $queue = "m9-postgres-restart-$($correlation.Substring(0,8))"
  $key = "m9-postgres-restart-$correlation"
  $workerId = "postgres-restart-worker-$($correlation.Substring(0,8))"
  $faultContainer = 'dse-m9-postgres-fault'
  $recoveryContainer = 'dse-m9-postgres-recovery'
  foreach ($container in @($faultContainer,$recoveryContainer)) {
    $existing = docker ps -a --filter "name=^/$container$" --format '{{.Names}}'
    if ($existing) { docker rm -f $container | Out-Null }
  }
  $before = Get-RuntimeSnapshot
  $openingEvidence = Join-Path $output "postgres_restart_open_$stamp.json"
  [ordered]@{topology=$before.services;before=$before;correlation_id=$correlation} |
    ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $openingEvidence -Encoding utf8
  $opened = Invoke-Probe @('open','--exercise','postgresql_restart_mid_retry_safe_task',
    '--incident-type','database_restart','--severity','critical','--evidence',$openingEvidence)
  $prepared = Invoke-Probe @('prepare-task','--task-name','simulation_day','--queue',$queue,
    '--idempotency-key',$key,'--correlation-id',$correlation,'--max-attempts','3','--pushes','1')
  $faultCode = @'
import os, time
from app.core.database import SessionLocal
from app.services.task_queue import RedisBroker, TaskWorker

def slow_handler(db, payload):
    time.sleep(20)
    return {'completed': True, 'verification_only': True}

broker = RedisBroker(os.environ['REDIS_URL'], os.environ['TASK_QUEUE_NAME'])
worker = TaskWorker(broker, worker_id=os.environ['M9_WORKER_ID'], handlers={'simulation_day': slow_handler})
with SessionLocal() as db:
    worker.heartbeat(db)
worker.run_once(timeout_seconds=10)
'@
  docker compose run -d --no-deps --name $faultContainer -e "TASK_QUEUE_NAME=$queue" `
    -e "M9_WORKER_ID=$workerId" `
    -e "DATABASE_URL=postgresql+psycopg://dse:$password@db:5432/$ValidationDatabase" `
    worker python -c $faultCode | Out-Null
  if ($LASTEXITCODE -ne 0) { throw 'Could not start PostgreSQL fault worker' }
  $leased = Wait-TaskState $key 'leased'
  $duringLease = Get-RuntimeSnapshot
  docker compose stop -t 10 db
  if ($LASTEXITCODE -ne 0) { throw 'PostgreSQL stop failed' }
  Start-Sleep -Seconds 25
  $duringOutage = Get-RuntimeSnapshot
  docker compose start db
  if ($LASTEXITCODE -ne 0) { throw 'PostgreSQL restart failed' }
  Wait-Service 'db' $true
  Wait-ContainerExit $faultContainer
  $afterRestart = Get-RuntimeSnapshot
  # The fault worker heartbeated immediately before leasing. Preserve the real stale threshold.
  Start-Sleep -Seconds 40
  $recovered = Invoke-Probe @('recover','--idempotency-key',$key,'--queue',$queue,
    '--stale-after-seconds','60')
  if ($recovered.recovered_workers -notcontains $workerId -or $recovered.task.state -ne 'retry') {
    throw 'PostgreSQL-outage lease recovery failed closed'
  }
  $recoveryCode = @'
import json, os
from app.services.task_queue import RedisBroker, TaskWorker
broker = RedisBroker(os.environ['REDIS_URL'], os.environ['TASK_QUEUE_NAME'])
worker = TaskWorker(broker, worker_id='postgres-restart-recovery')
result = worker.run_once(timeout_seconds=10)
print(json.dumps({'task_id': result.id if result else None, 'state': result.state if result else None, 'attempts': result.attempts if result else None}))
'@
  docker compose run -d --no-deps --name $recoveryContainer -e "TASK_QUEUE_NAME=$queue" `
    -e "DATABASE_URL=postgresql+psycopg://dse:$password@db:5432/$ValidationDatabase" `
    worker python -c $recoveryCode | Out-Null
  Wait-ContainerExit $recoveryContainer
  $recoveryLog = docker logs $recoveryContainer | ConvertFrom-Json
  $status = Wait-TaskState $key 'succeeded'
  $after = Get-RuntimeSnapshot
  $allServicesRunning = [bool](
    $after.services.Count -eq 5 -and
    -not @($after.services | Where-Object State -ne 'running').Count
  )
  $passed = [bool](
    $leased.task.lease_owner -eq $workerId -and
    $status.task.state -eq 'succeeded' -and
    [int]$status.task.attempts -eq 2 -and
    $recoveryLog.task_id -eq $prepared.task.id -and
    $allServicesRunning
  )
  $result = [ordered]@{
    exercise = 'postgresql_restart_mid_retry_safe_task'
    passed = $passed
    correlation_id = $correlation
    task_id = $prepared.task.id
    leased = $leased.task
    recovered = $recovered
    final_task = $status.task
    fault_worker_exit = docker inspect $faultContainer --format '{{json .State}}' | ConvertFrom-Json
    recovery_worker = $recoveryLog
    before = $before
    during_lease = $duringLease
    during_outage = $duringOutage
    after_restart = $afterRestart
    after = $after
    topology = $before.services
    orders = $status.orders
    fills = $status.fills
  }
  $resultPath = Join-Path $output "postgres_restart_result_$stamp.json"
  $result | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $resultPath -Encoding utf8
  Invoke-Probe @('resolve','--incident-id',$opened.incident_id,'--evidence',$resultPath) | Out-Null
  docker rm $faultContainer $recoveryContainer | Out-Null
  if (-not $passed) { throw 'PostgreSQL restart verification failed closed' }
  Write-Output ($result | ConvertTo-Json -Depth 12)
} finally {
  Remove-Item Env:DATABASE_URL -ErrorAction SilentlyContinue
  Remove-Item Env:REDIS_URL -ErrorAction SilentlyContinue
  Pop-Location
}
