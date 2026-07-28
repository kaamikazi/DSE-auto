param(
  [Parameter(Mandatory=$true)]
  [ValidateSet('competition')]
  [string]$Exercise,
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
  $services = @(docker compose --profile production-like ps --format json | ConvertFrom-Json |
    Select-Object Service,State,Health,ID)
  return [ordered]@{
    captured_at = (Get-Date).ToUniversalTime().ToString('o')
    available_gib = [math]::Round($memory.AvailableBytes / 1GB, 3)
    commit_headroom_gib = [math]::Round(($memory.CommitLimit - $memory.CommittedBytes) / 1GB, 3)
    pagefile_used_percent = [math]::Round($pagefile.CounterSamples[0].CookedValue, 2)
    disk_read_latency_ms = [math]::Round($disk.CounterSamples[0].CookedValue * 1000, 2)
    disk_queue_length = [math]::Round($disk.CounterSamples[1].CookedValue, 2)
    services = $services
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
    $state = (& docker inspect $Name --format '{{.State.Status}}' 2>$null).Trim()
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
  $env:DATABASE_ROLE = "simulation"
  $env:REDIS_URL = 'redis://127.0.0.1:6379/0'
  $stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
  $correlation = [guid]::NewGuid().ToString()
  $queue = "m9-competition-$($correlation.Substring(0,8))"
  $key = "m9-competition-$correlation"
  $effectKey = "m9-competition-effect-$correlation"
  $before = Get-RuntimeSnapshot
  $openingEvidence = Join-Path $output "competition_open_$stamp.json"
  [ordered]@{topology=$before.services;before=$before;correlation_id=$correlation} |
    ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $openingEvidence -Encoding utf8
  $incidentLabels = @(
    @{exercise='multi_worker_competition';type='worker_failure'},
    @{exercise='distributed_overlap_prevention';type='worker_failure'},
    @{exercise='duplicate_delivery_idempotency';type='corrupt_task_payload'}
  )
  $incidents = @()
  foreach ($label in $incidentLabels) {
    $opened = Invoke-Probe @('open','--exercise',$label.exercise,'--incident-type',$label.type,
      '--severity','medium','--evidence',$openingEvidence)
    $incidents += $opened.incident_id
  }
  $prepared = Invoke-Probe @('prepare-task','--task-name','verification_competition',
    '--queue',$queue,'--idempotency-key',$key,'--correlation-id',$correlation,
    '--max-attempts','3','--pushes','2')
  $workerCode = @'
import json, os
from app.services.events import emit_event
from app.services.task_queue import RedisBroker, TaskWorker

worker_id = os.environ['M9_WORKER_ID']
effect_key = os.environ['M9_EFFECT_KEY']
correlation_id = os.environ['M9_CORRELATION_ID']

def handler(db, payload):
    event = emit_event(
        db,
        'quote_received',
        aggregate_type='verification',
        aggregate_id=correlation_id,
        payload={'verification_only': True, 'worker_id': worker_id},
        idempotency_key=effect_key,
        correlation_id=correlation_id,
    )
    db.commit()
    return {'effect_event_id': event.id, 'worker_id': worker_id}

broker = RedisBroker(os.environ['REDIS_URL'], os.environ['TASK_QUEUE_NAME'])
worker = TaskWorker(broker, worker_id=worker_id, handlers={'verification_competition': handler})
result = worker.run_once(timeout_seconds=10)
print(json.dumps({'worker_id': worker_id, 'task_id': result.id if result else None, 'state': result.state if result else None}))
'@
  $containers = @('dse-m9-competition-a','dse-m9-competition-b')
  for ($index = 0; $index -lt $containers.Count; $index++) {
    $existingContainer = docker ps -a --filter "name=^/$($containers[$index])$" --format '{{.Names}}'
    if ($existingContainer) { docker rm -f $containers[$index] | Out-Null }
    docker compose run -d --no-deps --name $containers[$index] `
      -e "TASK_QUEUE_NAME=$queue" -e "M9_WORKER_ID=competition-worker-$index" `
      -e "M9_EFFECT_KEY=$effectKey" -e "M9_CORRELATION_ID=$correlation" `
      -e "DATABASE_URL=postgresql+psycopg://dse:$password@db:5432/$ValidationDatabase" `
      worker python -c $workerCode | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Could not start competition worker' }
  }
  foreach ($container in $containers) { Wait-ContainerExit $container }
  $workerResults = @($containers | ForEach-Object { docker logs $_ | ConvertFrom-Json })
  $status = Invoke-Probe @('task-status','--idempotency-key',$key,'--effect-key',$effectKey)
  $after = Get-RuntimeSnapshot
  $successfulWorkers = @($workerResults | Where-Object state -eq 'succeeded')
  $emptyWorkers = @($workerResults | Where-Object { -not $_.task_id })
  $passed = [bool](
    $status.task.state -eq 'succeeded' -and
    [int]$status.task.attempts -eq 1 -and
    [int]$status.effect_events -eq 1 -and
    $successfulWorkers.Count -eq 1 -and
    $emptyWorkers.Count -eq 1
  )
  $result = [ordered]@{
    exercise = 'competition_overlap_duplicate'
    passed = $passed
    correlation_id = $correlation
    task_id = $prepared.task.id
    task = $status.task
    worker_results = $workerResults
    effect_events = $status.effect_events
    orders = $status.orders
    fills = $status.fills
    before = $before
    after = $after
    topology = $before.services
  }
  $resultPath = Join-Path $output "competition_result_$stamp.json"
  $result | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $resultPath -Encoding utf8
  foreach ($incident in $incidents) {
    Invoke-Probe @('resolve','--incident-id',$incident,'--evidence',$resultPath) | Out-Null
  }
  foreach ($container in $containers) { docker rm $container | Out-Null }
  if (-not $passed) { throw 'Competition/overlap/duplicate verification failed closed' }
  Write-Output ($result | ConvertTo-Json -Depth 12)
} finally {
  Remove-Item Env:DATABASE_URL -ErrorAction SilentlyContinue
  Remove-Item Env:DATABASE_ROLE -ErrorAction SilentlyContinue
  Remove-Item Env:REDIS_URL -ErrorAction SilentlyContinue
  Pop-Location
}
