param(
  [Parameter(Mandatory=$true)]
  [ValidatePattern('^[a-zA-Z0-9_]+$')]
  [string]$ValidationDatabase,
  [string]$OutputDirectory = 'reports\distributed_campaign'
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$output = Join-Path $root $OutputDirectory
New-Item -ItemType Directory -Force -Path $output | Out-Null

function Invoke-Campaign([string[]]$Arguments) {
  Push-Location (Join-Path $root 'backend')
  try {
    $payload = & .\.venv\Scripts\python.exe ..\scripts\real_distributed_campaign.py @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Campaign probe failed: $($Arguments -join ' ')" }
    return $payload | ConvertFrom-Json
  } finally { Pop-Location }
}

function Wait-Day([string]$Campaign, [int]$Day, [int]$TimeoutSeconds = 60) {
  $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
  do {
    $status = Invoke-Campaign @('task-status','--campaign',$Campaign,'--day',"$Day")
    if ($status.state -eq 'succeeded') { return $status }
    if ($status.state -eq 'dead_letter') { throw "Campaign day $Day dead-lettered" }
    Start-Sleep -Seconds 1
  } while ((Get-Date) -lt $deadline)
  throw "Campaign day $Day did not complete"
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

function Get-Snapshot {
  $memory = Get-CimInstance Win32_PerfFormattedData_PerfOS_Memory
  return [ordered]@{
    captured_at = (Get-Date).ToUniversalTime().ToString('o')
    available_gib = [math]::Round($memory.AvailableBytes / 1GB, 3)
    commit_headroom_gib = [math]::Round(($memory.CommitLimit - $memory.CommittedBytes) / 1GB, 3)
    services = @(docker compose --profile production-like ps --format json | ConvertFrom-Json |
      Select-Object Service,State,Health,ID)
  }
}

function Assert-CampaignMemoryGate {
  & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $root 'scripts\memory_doctor.ps1')
  if ($LASTEXITCODE -ne 0) { throw 'Campaign memory diagnostic failed' }
  $report = Get-ChildItem (Join-Path $root 'reports\memory\memory_doctor_*.json') |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1
  $payload = Get-Content -LiteralPath $report.FullName -Raw | ConvertFrom-Json
  if (-not $payload.preflight.tiers.distributed_campaign.passed) {
    throw 'The unchanged 4 GiB/10 GiB distributed campaign gate failed'
  }
}

Push-Location $root
try {
  $passwordLine = Get-Content .env | Where-Object { $_ -like 'POSTGRES_PASSWORD=*' } |
    Select-Object -First 1
  if (-not $passwordLine) { throw 'POSTGRES_PASSWORD is not configured' }
  $password = $passwordLine.Substring('POSTGRES_PASSWORD='.Length)
  $env:POSTGRES_PASSWORD = $password
  $env:POSTGRES_VALIDATION_DATABASE = $ValidationDatabase
  $env:DATABASE_URL = "postgresql+psycopg://dse:$password@127.0.0.1:5432/$ValidationDatabase"
  $env:REDIS_URL = 'redis://127.0.0.1:6379/0'
  $stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
  $campaignName = "m9-real-distributed-$stamp"
  Assert-CampaignMemoryGate
  $campaign = Invoke-Campaign @('start','--campaign',$campaignName,'--total-days','10')
  $workerRestartPath = Join-Path $output "worker_restart_$stamp.json"

  for ($day = 1; $day -le 3; $day++) {
    $arguments = @('enqueue-day','--campaign',$campaignName,'--day',"$day")
    if ($day -eq 1) { $arguments += '--duplicate-delivery' }
    $enqueued = Invoke-Campaign $arguments
    $status = Wait-Day $campaignName $day
    if ([int]$status.attempts -ne 1 -or $status.task_id -ne $enqueued.task_id) {
      throw "Campaign day $day did not preserve idempotent exactly-once effect"
    }
    $completion = @('complete-day','--campaign',$campaignName,'--day',"$day")
    if ($day -eq 1) { $completion += '--stale-quote' }
    if ($day -eq 2) { $completion += @('--rejected-order','--partial-fill') }
    Invoke-Campaign $completion | Out-Null
    if ($day -eq 1) {
      $beforeRestart = Get-Snapshot
      docker compose kill worker
      if ($LASTEXITCODE -ne 0) { throw 'Campaign worker kill failed' }
      docker compose -f docker-compose.yml -f docker-compose.low-memory.yml `
        --profile production-like up -d worker
      if ($LASTEXITCODE -ne 0) { throw 'Campaign worker restart failed' }
      Wait-Service 'worker' $false
      $afterRestart = Get-Snapshot
      [ordered]@{
        passed=$true
        exercise='campaign_worker_restart'
        campaign_id=$campaign.campaign_id
        before=$beforeRestart
        after=$afterRestart
      } | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $workerRestartPath -Encoding utf8
      Invoke-Campaign @('record-worker-restart','--campaign',$campaignName,
        '--evidence',$workerRestartPath) | Out-Null
    }
  }

  $deadLetterStarted = Get-Date
  & powershell -NoProfile -ExecutionPolicy Bypass `
    -File (Join-Path $root 'scripts\run_dead_letter_retry_exercises.ps1') `
    -ValidationDatabase $ValidationDatabase
  if ($LASTEXITCODE -ne 0) { throw 'Campaign dead-letter recovery failed' }
  $deadLetterReport = Get-ChildItem (Join-Path $root 'reports\distributed_exercises\dead_retry_result_*.json') |
    Where-Object LastWriteTime -ge $deadLetterStarted | Sort-Object LastWriteTime -Descending |
    Select-Object -First 1
  if (-not $deadLetterReport) { throw 'Campaign dead-letter report was not created' }

  $backup3 = Join-Path $output "${campaignName}_day3.dump"
  & (Join-Path $root 'scripts\postgres_backup.ps1') -OutputPath $backup3 -Database $ValidationDatabase
  if ($LASTEXITCODE -ne 0) { throw 'Three-day campaign backup failed' }
  $report3 = Join-Path $output "${campaignName}_3_day.json"
  Invoke-Campaign @('checkpoint','--campaign',$campaignName,'--expected-days','3',
    '--backup',$backup3,'--dead-letter-report',$deadLetterReport.FullName,'--output',$report3) |
    Out-Null

  Assert-CampaignMemoryGate
  for ($day = 4; $day -le 10; $day++) {
    $enqueued = Invoke-Campaign @('enqueue-day','--campaign',$campaignName,'--day',"$day")
    $status = Wait-Day $campaignName $day
    if ([int]$status.attempts -ne 1 -or $status.task_id -ne $enqueued.task_id) {
      throw "Campaign extension day $day did not complete exactly once"
    }
    Invoke-Campaign @('complete-day','--campaign',$campaignName,'--day',"$day") | Out-Null
  }
  $backup10 = Join-Path $output "${campaignName}_day10.dump"
  & (Join-Path $root 'scripts\postgres_backup.ps1') -OutputPath $backup10 -Database $ValidationDatabase
  if ($LASTEXITCODE -ne 0) { throw 'Ten-day campaign backup failed' }
  $report10 = Join-Path $output "${campaignName}_10_day.json"
  $final = Invoke-Campaign @('checkpoint','--campaign',$campaignName,'--expected-days','10',
    '--backup',$backup10,'--dead-letter-report',$deadLetterReport.FullName,
    '--output',$report10,'--final')
  Write-Output ($final | ConvertTo-Json -Depth 12)
} finally {
  Remove-Item Env:DATABASE_URL -ErrorAction SilentlyContinue
  Remove-Item Env:REDIS_URL -ErrorAction SilentlyContinue
  Remove-Item Env:POSTGRES_VALIDATION_DATABASE -ErrorAction SilentlyContinue
  Pop-Location
}
