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

function Wait-ContainerExit([string]$Name, [int]$TimeoutSeconds = 60) {
  $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
  do {
    $state = (& docker inspect $Name --format '{{.State.Status}}').Trim()
    if ($state -in @('exited','dead')) { return }
    Start-Sleep -Seconds 1
  } while ((Get-Date) -lt $deadline)
  throw "Verification container did not exit: $Name"
}

function Get-TaskStatus([string]$Key) {
  return Invoke-Probe @('task-status','--idempotency-key',$Key)
}

Push-Location $root
try {
  $passwordLine = Get-Content .env | Where-Object { $_ -like 'POSTGRES_PASSWORD=*' } |
    Select-Object -First 1
  if (-not $passwordLine) { throw 'POSTGRES_PASSWORD is not configured' }
  $password = $passwordLine.Substring('POSTGRES_PASSWORD='.Length)
  $env:DATABASE_URL = "postgresql+psycopg://dse:$password@127.0.0.1:5432/$ValidationDatabase"
  $env:DATABASE_ROLE = "simulation"
  $env:REDIS_URL = 'redis://127.0.0.1:6379/0'
  $stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
  $correlation = [guid]::NewGuid().ToString()
  $queue = "m9-dead-retry-$($correlation.Substring(0,8))"
  $deadKey = "m9-dead-letter-$correlation"
  $retryKey = "m9-retry-$correlation"
  $containers = @('dse-m9-dead-create','dse-m9-dead-replay','dse-m9-retry')
  foreach ($container in $containers) {
    $existing = docker ps -a --filter "name=^/$container$" --format '{{.Names}}'
    if ($existing) { docker rm -f $container | Out-Null }
  }
  $before = Get-RuntimeSnapshot
  $openingEvidence = Join-Path $output "dead_retry_open_$stamp.json"
  [ordered]@{topology=$before.services;before=$before;correlation_id=$correlation} |
    ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $openingEvidence -Encoding utf8
  $incidentLabels = @(
    @{exercise='dead_letter_creation';type='dead_letter_accumulation';severity='high'},
    @{exercise='dead_letter_replay';type='corrupt_task_payload';severity='medium'},
    @{exercise='real_retry_path';type='worker_failure';severity='medium'}
  )
  $incidents = @()
  foreach ($label in $incidentLabels) {
    $opened = Invoke-Probe @('open','--exercise',$label.exercise,'--incident-type',$label.type,
      '--severity',$label.severity,'--evidence',$openingEvidence)
    $incidents += $opened.incident_id
  }

  $deadPrepared = Invoke-Probe @('prepare-task','--task-name','verification_dead_letter',
    '--queue',$queue,'--idempotency-key',$deadKey,'--correlation-id',$correlation,
    '--max-attempts','1','--pushes','1')
  $deadCode = @'
import json, os
from app.services.task_queue import RedisBroker, TaskWorker

def fail(db, payload):
    raise RuntimeError('intentional verification dead letter')

broker = RedisBroker(os.environ['REDIS_URL'], os.environ['TASK_QUEUE_NAME'])
worker = TaskWorker(broker, worker_id='dead-letter-creator', handlers={'verification_dead_letter': fail})
result = worker.run_once(timeout_seconds=10)
print(json.dumps({'task_id': result.id if result else None, 'state': result.state if result else None, 'attempts': result.attempts if result else None}))
'@
  docker compose run -d --no-deps --name $containers[0] -e "TASK_QUEUE_NAME=$queue" `
    -e "DATABASE_URL=postgresql+psycopg://dse:$password@db:5432/$ValidationDatabase" `
    worker python -c $deadCode | Out-Null
  Wait-ContainerExit $containers[0]
  $deadCreateLog = docker logs $containers[0] | ConvertFrom-Json
  $deadStatus = Get-TaskStatus $deadKey
  if ($deadStatus.task.state -ne 'dead_letter' -or [int]$deadStatus.task.attempts -ne 1) {
    throw 'Dead-letter creation failed closed'
  }

  $replayed = Invoke-Probe @('replay','--idempotency-key',$deadKey,'--queue',$queue)
  $replayCode = @'
import json, os
from app.services.task_queue import RedisBroker, TaskWorker

def recovered(db, payload):
    return {'replayed': True, 'verification_only': True}

broker = RedisBroker(os.environ['REDIS_URL'], os.environ['TASK_QUEUE_NAME'])
worker = TaskWorker(broker, worker_id='dead-letter-replayer', handlers={'verification_dead_letter': recovered})
result = worker.run_once(timeout_seconds=10)
print(json.dumps({'task_id': result.id if result else None, 'state': result.state if result else None, 'attempts': result.attempts if result else None}))
'@
  docker compose run -d --no-deps --name $containers[1] -e "TASK_QUEUE_NAME=$queue" `
    -e "DATABASE_URL=postgresql+psycopg://dse:$password@db:5432/$ValidationDatabase" `
    worker python -c $replayCode | Out-Null
  Wait-ContainerExit $containers[1]
  $deadReplayLog = docker logs $containers[1] | ConvertFrom-Json
  $replayedStatus = Get-TaskStatus $deadKey
  if ($replayedStatus.task.state -ne 'succeeded' -or [int]$replayedStatus.task.attempts -ne 2) {
    throw 'Dead-letter replay failed closed'
  }

  $retryPrepared = Invoke-Probe @('prepare-task','--task-name','verification_retry',
    '--queue',$queue,'--idempotency-key',$retryKey,'--correlation-id',$correlation,
    '--max-attempts','3','--pushes','1')
  $retryCode = @'
import json, os, time
from app.services.task_queue import RedisBroker, TaskWorker

calls = {'count': 0}
def retry_once(db, payload):
    calls['count'] += 1
    if calls['count'] == 1:
        raise RuntimeError('intentional retryable verification failure')
    return {'retried': True, 'handler_calls': calls['count']}

broker = RedisBroker(os.environ['REDIS_URL'], os.environ['TASK_QUEUE_NAME'])
worker = TaskWorker(broker, worker_id='retry-verifier', handlers={'verification_retry': retry_once})
first = worker.run_once(timeout_seconds=10)
time.sleep(2)
second = worker.run_once(timeout_seconds=10)
print(json.dumps({'first_state': first.state if first else None, 'second_state': second.state if second else None, 'task_id': second.id if second else None, 'attempts': second.attempts if second else None}))
'@
  docker compose run -d --no-deps --name $containers[2] -e "TASK_QUEUE_NAME=$queue" `
    -e "DATABASE_URL=postgresql+psycopg://dse:$password@db:5432/$ValidationDatabase" `
    worker python -c $retryCode | Out-Null
  Wait-ContainerExit $containers[2]
  $retryLog = docker logs $containers[2] | ConvertFrom-Json
  $retryStatus = Get-TaskStatus $retryKey
  $after = Get-RuntimeSnapshot
  $passed = [bool](
    $deadCreateLog.state -eq 'dead_letter' -and
    $deadReplayLog.state -eq 'succeeded' -and
    $replayedStatus.task.result.replayed -eq $true -and
    $retryStatus.task.state -eq 'succeeded' -and
    [int]$retryStatus.task.attempts -eq 2 -and
    $retryStatus.task.result.retried -eq $true -and
    $retryLog.second_state -eq 'succeeded'
  )
  $result = [ordered]@{
    exercise = 'dead_letter_replay_retry'
    passed = $passed
    correlation_id = $correlation
    dead_letter_task_id = $deadPrepared.task.id
    dead_letter_created = $deadStatus.task
    dead_letter_replayed = $replayedStatus.task
    retry_task_id = $retryPrepared.task.id
    retry_task = $retryStatus.task
    worker_logs = [ordered]@{create=$deadCreateLog;replay=$deadReplayLog;retry=$retryLog}
    replay_command = $replayed
    before = $before
    after = $after
    topology = $before.services
    orders = $retryStatus.orders
    fills = $retryStatus.fills
  }
  $resultPath = Join-Path $output "dead_retry_result_$stamp.json"
  $result | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $resultPath -Encoding utf8
  foreach ($incident in $incidents) {
    Invoke-Probe @('resolve','--incident-id',$incident,'--evidence',$resultPath) | Out-Null
  }
  docker rm $containers | Out-Null
  if (-not $passed) { throw 'Dead-letter/retry verification failed closed' }
  Write-Output ($result | ConvertTo-Json -Depth 12)
} finally {
  Remove-Item Env:DATABASE_URL -ErrorAction SilentlyContinue
  Remove-Item Env:DATABASE_ROLE -ErrorAction SilentlyContinue
  Remove-Item Env:REDIS_URL -ErrorAction SilentlyContinue
  Pop-Location
}
