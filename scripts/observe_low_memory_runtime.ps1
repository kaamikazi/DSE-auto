param(
  [Parameter(Mandatory=$true)]
  [string]$StageLabel,
  [Parameter(Mandatory=$true)]
  [string[]]$Services,
  [ValidateRange(600, 3600)]
  [int]$DurationSeconds = 600,
  [ValidateRange(15, 120)]
  [int]$IntervalSeconds = 30,
  [string]$OutputDirectory = "reports\memory"
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$output = Join-Path $root $OutputDirectory
New-Item -ItemType Directory -Force -Path $output | Out-Null
$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$rawPath = Join-Path $output "runtime_${StageLabel}_raw_$stamp.json"
$resultPath = Join-Path $output "runtime_${StageLabel}_result_$stamp.json"

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
  $pagefile = Get-CimInstance Win32_PerfFormattedData_PerfOS_PagingFile -Filter "Name='_Total'"
  $pagefileUsage = @(Get-CimInstance Win32_PageFileUsage)
  $serviceSnapshots = @($Services | ForEach-Object { Get-ServiceSnapshot $_ })
  $containerMemory = (($serviceSnapshots | Measure-Object memory_gib -Sum).Sum)
  $vmmem = Get-Process -Name vmmemWSL,vmmem -ErrorAction SilentlyContinue
  $vmmemGiB = [math]::Round((($vmmem | Measure-Object WorkingSet64 -Sum).Sum) / 1GB, 4)
  $restartMap = @{}
  foreach ($serviceSnapshot in $serviceSnapshots) {
    $restartMap[$serviceSnapshot.service] = $serviceSnapshot.restarts
  }
  $elapsed = ((Get-Date) - $started).TotalSeconds
  $samples += [pscustomobject]@{
    captured_at = (Get-Date).ToUniversalTime().ToString('o')
    elapsed_seconds = [math]::Round($elapsed, 1)
    available_gib = [math]::Round($memory.AvailableBytes / 1GB, 3)
    commit_headroom_gib = [math]::Round(($memory.CommitLimit - $memory.CommittedBytes) / 1GB, 3)
    pagefile_used_gib = [math]::Round((($pagefileUsage | Measure-Object CurrentUsage -Sum).Sum) / 1024, 3)
    pagefile_used_percent = [math]::Round([double]$pagefile.PercentUsage, 2)
    hard_paging_per_second = [math]::Round([double]$memory.PageReadsPersec + [double]$memory.PageWritesPersec, 2)
    container_memory_gib = [math]::Round($containerMemory, 4)
    wsl_working_set_gib = $vmmemGiB
    docker_wsl_overhead_gib = [math]::Round([math]::Max($vmmemGiB - $containerMemory, 0), 4)
    services = $serviceSnapshots
    container_restarts = $restartMap
    oom_killed = [bool]($serviceSnapshots | Where-Object oom_killed)
    process_missing = [bool]($serviceSnapshots | Where-Object { -not $_.running })
  }
  if ($elapsed -ge $DurationSeconds) { break }
  Start-Sleep -Seconds ([math]::Min($IntervalSeconds, $DurationSeconds - [int]$elapsed))
} while ($true)

$databaseHealthy = $false
& docker compose exec -T db pg_isready -U dse -d dse_autotrader 2>$null
$databaseHealthy = $LASTEXITCODE -eq 0
$auditValid = Test-AuditValidity $Services
$peakContainer = ($samples | Measure-Object container_memory_gib -Maximum).Maximum
$peakOverhead = ($samples | Measure-Object docker_wsl_overhead_gib -Maximum).Maximum
$raw = [ordered]@{
  generated_at = (Get-Date).ToUniversalTime().ToString('o')
  stage = $StageLabel
  expected_services = $Services
  duration_seconds = $DurationSeconds
  interval_seconds = $IntervalSeconds
  project_footprint_gib = [math]::Round($peakContainer + $peakOverhead, 4)
  peak_container_memory_gib = $peakContainer
  peak_docker_wsl_overhead_gib = $peakOverhead
  database_healthy = $databaseHealthy
  audit_valid = $auditValid
  samples = $samples
}
$raw | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $rawPath -Encoding utf8

Push-Location $root
try {
  & backend\.venv\Scripts\python.exe scripts\evaluate_runtime_observation.py --input $rawPath --output $resultPath
  exit $LASTEXITCODE
} finally {
  Pop-Location
}
