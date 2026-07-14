param(
  [string]$OutputDirectory = "reports\infrastructure",
  [ValidateSet("database_only", "integration_tests", "distributed_runtime", "distributed_campaign")]
  [string]$WorkloadTier = "distributed_campaign",
  [switch]$ExpectApplicationPorts
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$output = Join-Path $root $OutputDirectory
Push-Location $root
try {
  $arguments = @("scripts\operator.py", "infrastructure", "doctor", "--output-dir", $output, "--workload-tier", $WorkloadTier)
  if ($ExpectApplicationPorts) { $arguments += "--expect-application-ports" }
  & backend\.venv\Scripts\python.exe @arguments
  if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
} finally {
  Pop-Location
}
