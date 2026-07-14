param(
  [Parameter(Mandatory=$true)]
  [ValidateSet('B1','B2','B3')]
  [string]$Stage,
  [Parameter(Mandatory=$true)]
  [ValidatePattern('^[a-zA-Z0-9_]+$')]
  [string]$ValidationDatabase
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$compose = @('-f','docker-compose.yml','-f','docker-compose.low-memory.yml','--profile','production-like')
$expected = @{
  B1 = @('db','redis','backend','scheduler','worker')
  B2 = @('db','redis','scheduler','worker','worker_2')
  B3 = @('db','redis','backend','scheduler','worker')
}[$Stage]
$application = @($expected | Where-Object { $_ -notin @('db','redis') })
$unused = @(@('backend','scheduler','worker','worker_2') | Where-Object { $_ -notin $application })
$started = $false

function Wait-Substage([string[]]$Services, [int]$TimeoutSeconds = 120) {
  $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
  do {
    $items = @(& docker compose @compose ps --format json $Services | ConvertFrom-Json)
    $byService = @{}
    foreach ($item in $items) { $byService[$item.Service] = $item }
    $ready = $byService.Count -eq $Services.Count
    foreach ($service in $Services) {
      $item = $byService[$service]
      if (-not $item -or $item.State -ne 'running') { $ready = $false; continue }
      if ($service -in @('db','redis','backend') -and $item.Health -ne 'healthy') {
        $ready = $false
      }
    }
    if ($ready) { return }
    Start-Sleep -Seconds 3
  } while ((Get-Date) -lt $deadline)
  throw "Timed out waiting for low-memory sub-stage $Stage"
}

Push-Location $root
try {
  if (-not $env:POSTGRES_PASSWORD) {
    $passwordLine = Get-Content .env | Where-Object { $_ -like 'POSTGRES_PASSWORD=*' } |
      Select-Object -First 1
    if (-not $passwordLine) { throw 'POSTGRES_PASSWORD is not configured' }
    $env:POSTGRES_PASSWORD = $passwordLine.Substring('POSTGRES_PASSWORD='.Length)
  }
  & powershell -NoProfile -ExecutionPolicy Bypass -File scripts\memory_doctor.ps1
  if ($LASTEXITCODE -ne 0) { throw 'Pre-start memory diagnostic failed' }
  $memoryReport = Get-ChildItem reports\memory\memory_doctor_*.json |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1
  $memory = Get-Content -LiteralPath $memoryReport.FullName -Raw | ConvertFrom-Json
  if (-not $memory.preflight.tiers.distributed_runtime.passed) {
    throw 'The unchanged 3 GiB pre-start gate failed; no services were changed'
  }

  $env:POSTGRES_VALIDATION_DATABASE = $ValidationDatabase
  & docker compose @compose up -d db redis
  if ($LASTEXITCODE -ne 0) { throw 'Could not start the required data services' }
  Wait-Substage @('db','redis')
  & powershell -NoProfile -ExecutionPolicy Bypass -File scripts\infrastructure_doctor.ps1 -WorkloadTier distributed_runtime
  if ($LASTEXITCODE -ne 0) { throw 'The unchanged 3 GiB pre-start gate failed' }

  $env:DATABASE_URL = "postgresql+psycopg://dse:$env:POSTGRES_PASSWORD@127.0.0.1:5432/$ValidationDatabase"
  Push-Location backend
  try {
    .\.venv\Scripts\python.exe ..\scripts\operator.py verify-audit
    if ($LASTEXITCODE -ne 0) { throw 'Validation database audit verification failed' }
  } finally { Pop-Location }

  $stopServices = @('db_test') + $unused
  docker compose stop @stopServices
  if ($LASTEXITCODE -ne 0) { throw 'Could not stop sub-stage exclusions' }
  & docker compose @compose up -d @expected
  if ($LASTEXITCODE -ne 0) { throw "Low-memory sub-stage $Stage startup failed" }
  $started = $true
  Wait-Substage $expected
  & docker compose @compose ps
  Write-Output "Low-memory sub-stage $Stage started against verified isolated database $ValidationDatabase."
} catch {
  if ($started) { & docker compose @compose stop -t 30 @application | Out-Null }
  throw
} finally {
  Remove-Item Env:DATABASE_URL -ErrorAction SilentlyContinue
  Pop-Location
}
