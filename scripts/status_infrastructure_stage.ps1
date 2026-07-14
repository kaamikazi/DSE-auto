param(
  [ValidateSet("database_only", "integration_tests", "distributed_runtime", "distributed_campaign")]
  [string]$WorkloadTier = "database_only"
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Push-Location $root
try {
  docker compose --profile production-like ps
  docker stats --no-stream
  & powershell -NoProfile -ExecutionPolicy Bypass -File scripts\memory_doctor.ps1
  & powershell -NoProfile -ExecutionPolicy Bypass -File scripts\infrastructure_doctor.ps1 -WorkloadTier $WorkloadTier
  exit $LASTEXITCODE
} finally {
  Pop-Location
}
