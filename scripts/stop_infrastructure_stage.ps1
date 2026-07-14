param(
  [Parameter(Mandatory=$true)]
  [ValidateSet("A", "B", "C")]
  [string]$Stage
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Push-Location $root
try {
  if ($Stage -eq "A") {
    docker compose stop -t 30 db_test
  } else {
    docker compose --profile production-like stop -t 30 worker_2 worker scheduler backend
  }
  if ($LASTEXITCODE -ne 0) { throw "Stage $Stage stop failed" }
  Write-Output "Stage $Stage processes stopped gracefully; database volumes were preserved."
} finally {
  Pop-Location
}
