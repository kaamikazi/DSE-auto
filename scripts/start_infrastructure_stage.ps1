param(
  [Parameter(Mandatory=$true)]
  [ValidateSet("A", "B", "C")]
  [string]$Stage
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$tier = @{ A = "integration_tests"; B = "distributed_runtime"; C = "distributed_campaign" }[$Stage]
$applicationProcessesStarted = $false
function Wait-ComposeHealthy([string[]]$Services, [int]$TimeoutSeconds = 90) {
  $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
  do {
    $items = @(docker compose ps --format json $Services | ConvertFrom-Json)
    $healthy = $items.Count -eq $Services.Count -and -not @($items | Where-Object {
      $_.State -ne "running" -or $_.Health -ne "healthy"
    })
    if ($healthy) { return }
    Start-Sleep -Seconds 2
  } while ((Get-Date) -lt $deadline)
  throw "Timed out waiting for required Compose health checks: $($Services -join ', ')"
}
Push-Location $root
try {
  & powershell -NoProfile -ExecutionPolicy Bypass -File scripts\memory_doctor.ps1
  if ($LASTEXITCODE -ne 0) { throw "Memory diagnostic failed" }
  $memoryReport = Get-ChildItem reports\memory\memory_doctor_*.json |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1
  $memory = Get-Content -LiteralPath $memoryReport.FullName -Raw | ConvertFrom-Json
  if (-not $memory.preflight.tiers.$tier.passed) {
    throw "Memory gate for $tier failed; no stage services were changed"
  }

  if ($Stage -eq "A") {
    docker compose up -d db db_test redis
    if ($LASTEXITCODE -ne 0) { throw "Stage A service startup failed" }
    Wait-ComposeHealthy @("db", "db_test", "redis")
  } else {
    docker compose stop db_test
    if ($LASTEXITCODE -ne 0) { throw "Could not stop test-only PostgreSQL service" }
    & powershell -NoProfile -ExecutionPolicy Bypass -File scripts\infrastructure_doctor.ps1 -WorkloadTier $tier
    if ($LASTEXITCODE -ne 0) { throw "Infrastructure gate for Stage $Stage failed" }
    docker compose --profile production-like up -d db redis backend scheduler worker worker_2
    if ($LASTEXITCODE -ne 0) { throw "Stage $Stage service startup failed" }
    $applicationProcessesStarted = $true
  }

  $postArguments = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "scripts\infrastructure_doctor.ps1", "-WorkloadTier", $tier)
  if ($Stage -ne "A") { $postArguments += "-ExpectApplicationPorts" }
  & powershell @postArguments
  if ($LASTEXITCODE -ne 0) { throw "Post-start readiness for Stage $Stage failed" }
  Write-Output "Infrastructure Stage $Stage is ready for PAPER-only verification."
} catch {
  if ($applicationProcessesStarted) {
    docker compose --profile production-like stop -t 30 worker_2 worker scheduler backend | Out-Null
  }
  throw
} finally {
  Pop-Location
}
