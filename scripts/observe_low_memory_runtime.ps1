param(
  [Parameter(Mandatory=$true)]
  [string]$StageLabel,
  [Parameter(Mandatory=$true)]
  [string]$Services,
  [ValidateRange(15, 3600)]
  [int]$DurationSeconds = 600,
  [ValidateRange(15, 120)]
  [int]$IntervalSeconds = 30,
  [ValidateRange(120, 600)]
  [int]$WarmupSeconds = 120,
  [ValidateSet('Runtime','Shutdown')]
  [string]$Mode = 'Runtime',
  [string]$OutputDirectory = "reports\memory"
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$output = Join-Path $root $OutputDirectory
New-Item -ItemType Directory -Force -Path $output | Out-Null
$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$rawPath = Join-Path $output "runtime_${StageLabel}_raw_$stamp.json"
$resultPath = Join-Path $output "runtime_${StageLabel}_result_$stamp.json"
$expectedServices = @($Services.Split(',') | ForEach-Object { $_.Trim() } | Where-Object { $_ })
if ($expectedServices.Count -eq 0) { throw 'At least one expected service is required' }
if ($Mode -eq 'Runtime' -and $DurationSeconds -lt 600) {
  throw 'Runtime mode requires at least 600 steady-state seconds after warm-up'
}

function Convert-MemoryToGiB([string]$Value) {
  if ($Value -notmatch '^\s*([0-9.]+)\s*([KMG]iB)') { return 0.0 }
  $number = [double]$Matches[1]
  return [math]::Round($(switch ($Matches[2]) {
    'KiB' { $number / 1MB }
    'MiB' { $number / 1024 }
    'GiB' { $number }
  }), 4)
}

function Get-ServiceSnapshot([string]$Service) {
  $id = (& docker compose ps -q $Service).Trim()
  if (-not $id) {
    return [pscustomobject]@{ service=$Service; running=$false; memory_gib=0; restarts=0; oom_killed=$false }
  }
  $state = & docker inspect $id --format '{{json .State}}' | ConvertFrom-Json
  $restarts = [int](& docker inspect $id --format '{{.RestartCount}}')
  $memory = 0.0
  if ($state.Running) {
    $stats = & docker stats $id --no-stream --format json | ConvertFrom-Json
    $memory = Convert-MemoryToGiB ([string]$stats.MemUsage)
  }
  return [pscustomobject]@{
    service = $Service
    running = [bool]$state.Running
    memory_gib = $memory
    restarts = $restarts
    oom_killed = [bool]$state.OOMKilled
  }
}

function Get-CounterValue([object[]]$Samples, [string]$Suffix) {
  $normalized = $Suffix.ToLowerInvariant()
  $match = $Samples | Where-Object { $_.Path.ToLowerInvariant().EndsWith($normalized) } |
    Select-Object -First 1
  if (-not $match) { throw "Windows performance counter was unavailable: $Suffix" }
  return [double]$match.CookedValue
}

function Get-PagingSnapshot {
  $paths = @(
    '\Memory\Page Faults/sec',
    '\Memory\Pages Input/sec',
    '\Memory\Page Reads/sec',
    '\Paging File(_Total)\% Usage',
    '\PhysicalDisk(_Total)\Avg. Disk sec/Read',
    '\PhysicalDisk(_Total)\Avg. Disk sec/Write',
    '\PhysicalDisk(_Total)\Avg. Disk Queue Length',
    '\PhysicalDisk(_Total)\Current Disk Queue Length'
  )
  $samples = @(Get-Counter -Counter $paths -MaxSamples 1).CounterSamples
  return [pscustomobject]@{
    page_faults_per_second = Get-CounterValue $samples '\memory\page faults/sec'
    pages_input_per_second = Get-CounterValue $samples '\memory\pages input/sec'
    page_reads_per_second = Get-CounterValue $samples '\memory\page reads/sec'
    pagefile_used_percent = Get-CounterValue $samples '\paging file(_total)\% usage'
    disk_read_latency_ms = 1000 * (Get-CounterValue $samples '\physicaldisk(_total)\avg. disk sec/read')
    disk_write_latency_ms = 1000 * (Get-CounterValue $samples '\physicaldisk(_total)\avg. disk sec/write')
    disk_queue_length = Get-CounterValue $samples '\physicaldisk(_total)\avg. disk queue length'
    current_disk_queue_length = Get-CounterValue $samples '\physicaldisk(_total)\current disk queue length'
  }
}

function Get-OperationalSignals([string[]]$ExpectedServices) {
  $candidate = @('scheduler','worker','worker_2','backend') |
    Where-Object { $ExpectedServices -contains $_ -and ((& docker compose ps -q $_).Trim()) } |
    Select-Object -First 1
  if (-not $candidate) {
    return [pscustomobject]@{ scheduler_lag_seconds=$null; worker_heartbeat_delay_seconds=$null }
  }
  $command = "import json; from app.core.database import SessionLocal; from app.services.runtime_observation import runtime_operational_delays; db=SessionLocal(); print(json.dumps(runtime_operational_delays(db))); db.close()"
  $payload = & docker compose exec -T $candidate python -c $command 2>$null
  if ($LASTEXITCODE -ne 0) {
    return [pscustomobject]@{ scheduler_lag_seconds=$null; worker_heartbeat_delay_seconds=$null }
  }
  return $payload | ConvertFrom-Json
}

function Test-AuditValidity([string[]]$ExpectedServices) {
  $candidate = @('backend','scheduler','worker','worker_2') |
    Where-Object { $ExpectedServices -contains $_ -and ((& docker compose ps -q $_).Trim()) } |
    Select-Object -First 1
  if (-not $candidate) { return $false }
  & docker compose exec -T $candidate python -c "from app.core.database import SessionLocal; from app.services.audit import verify_audit_chain; db=SessionLocal(); ok=verify_audit_chain(db); db.close(); raise SystemExit(0 if ok else 2)" 2>$null
  return $LASTEXITCODE -eq 0
}

$samples = @()
$started = Get-Date
do {
  $memory = Get-CimInstance Win32_PerfFormattedData_PerfOS_Memory
  $pagefileUsage = @(Get-CimInstance Win32_PageFileUsage)
  $paging = Get-PagingSnapshot
  $signals = Get-OperationalSignals $expectedServices
  $serviceSnapshots = @($expectedServices | ForEach-Object { Get-ServiceSnapshot $_ })
  $containerMemory = (($serviceSnapshots | Measure-Object memory_gib -Sum).Sum)
  $vmmem = Get-Process -Name vmmemWSL,vmmem -ErrorAction SilentlyContinue
  $vmmemGiB = [math]::Round((($vmmem | Measure-Object WorkingSet64 -Sum).Sum) / 1GB, 4)
  $restartMap = @{}
  foreach ($serviceSnapshot in $serviceSnapshots) {
    $restartMap[$serviceSnapshot.service] = $serviceSnapshot.restarts
  }
  $elapsed = ((Get-Date) - $started).TotalSeconds
  $phase = if ($Mode -eq 'Shutdown') {
    'shutdown_activity'
  } elseif ($elapsed -lt $WarmupSeconds) {
    'startup_warmup'
  } else {
    'steady_state'
  }
  $samples += [pscustomobject]@{
    captured_at = (Get-Date).ToUniversalTime().ToString('o')
    elapsed_seconds = [math]::Round($elapsed, 1)
    phase = $phase
    available_gib = [math]::Round($memory.AvailableBytes / 1GB, 3)
    commit_headroom_gib = [math]::Round(($memory.CommitLimit - $memory.CommittedBytes) / 1GB, 3)
    pagefile_used_gib = [math]::Round((($pagefileUsage | Measure-Object CurrentUsage -Sum).Sum) / 1024, 3)
    pagefile_used_percent = [math]::Round($paging.pagefile_used_percent, 2)
    page_faults_per_second = [math]::Round($paging.page_faults_per_second, 2)
    pages_input_per_second = [math]::Round($paging.pages_input_per_second, 2)
    page_reads_per_second = [math]::Round($paging.page_reads_per_second, 2)
    disk_read_latency_ms = [math]::Round($paging.disk_read_latency_ms, 2)
    disk_write_latency_ms = [math]::Round($paging.disk_write_latency_ms, 2)
    disk_queue_length = [math]::Round($paging.disk_queue_length, 2)
    current_disk_queue_length = [math]::Round($paging.current_disk_queue_length, 2)
    scheduler_lag_seconds = $signals.scheduler_lag_seconds
    worker_heartbeat_delay_seconds = $signals.worker_heartbeat_delay_seconds
    container_memory_gib = [math]::Round($containerMemory, 4)
    wsl_working_set_gib = $vmmemGiB
    docker_wsl_overhead_gib = [math]::Round([math]::Max($vmmemGiB - $containerMemory, 0), 4)
    services = $serviceSnapshots
    container_restarts = $restartMap
    oom_killed = [bool]($serviceSnapshots | Where-Object oom_killed)
    process_missing = [bool]($serviceSnapshots | Where-Object { -not $_.running })
  }

  if ($Mode -eq 'Shutdown') {
    $measuredDuration = $elapsed - [double]$samples[0].elapsed_seconds
  } else {
    $steadySamples = @($samples | Where-Object phase -eq 'steady_state')
    $measuredDuration = if ($steadySamples.Count) {
      $elapsed - [double]$steadySamples[0].elapsed_seconds
    } else { 0 }
  }
  if ($measuredDuration -ge $DurationSeconds) { break }
  $remaining = [math]::Max($DurationSeconds - $measuredDuration, 1)
  Start-Sleep -Seconds ([math]::Min($IntervalSeconds, [int][math]::Ceiling($remaining)))
} while ($true)

$databaseHealthy = $false
& docker compose exec -T db pg_isready -U dse -d dse_autotrader 2>$null
$databaseHealthy = $LASTEXITCODE -eq 0
$auditValid = Test-AuditValidity $expectedServices
$peakContainer = ($samples | Measure-Object container_memory_gib -Maximum).Maximum
$peakOverhead = ($samples | Measure-Object docker_wsl_overhead_gib -Maximum).Maximum
$raw = [ordered]@{
  generated_at = (Get-Date).ToUniversalTime().ToString('o')
  stage = $StageLabel
  mode = $Mode.ToLowerInvariant()
  counter_api = 'Get-Counter cooked Windows performance counters'
  expected_services = $expectedServices
  warmup_seconds = $(if ($Mode -eq 'Runtime') { $WarmupSeconds } else { 0 })
  steady_state_seconds = $(if ($Mode -eq 'Runtime') { $DurationSeconds } else { 0 })
  shutdown_observation_seconds = $(if ($Mode -eq 'Shutdown') { $DurationSeconds } else { 0 })
  project_footprint_gib = [math]::Round($peakContainer + $peakOverhead, 4)
  peak_container_memory_gib = $peakContainer
  peak_docker_wsl_overhead_gib = $peakOverhead
  database_healthy = $databaseHealthy
  audit_valid = $auditValid
  samples = $samples
}
$raw | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $rawPath -Encoding utf8

if ($Mode -eq 'Shutdown') {
  Write-Output "Read-only shutdown activity evidence: $rawPath"
  exit 0
}

Push-Location $root
try {
  & backend\.venv\Scripts\python.exe scripts\evaluate_runtime_observation.py --input $rawPath --output $resultPath
  exit $LASTEXITCODE
} finally {
  Pop-Location
}
