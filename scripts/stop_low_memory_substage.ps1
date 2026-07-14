$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Push-Location $root
try {
  docker compose --profile production-like stop -t 30 worker_2 worker scheduler backend
  if ($LASTEXITCODE -ne 0) { throw 'Application process shutdown failed' }
  Write-Output 'Application processes stopped; PostgreSQL, Redis, and every volume were preserved.'
} finally { Pop-Location }
