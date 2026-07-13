$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$logDir = Join-Path $root "logs\production-like"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
Push-Location $root
try {
  docker compose --profile production-like logs --no-color | Set-Content -Encoding utf8 (Join-Path $logDir "shutdown-$(Get-Date -Format yyyyMMdd_HHmmss).log")
  docker compose --profile production-like stop -t 30 frontend scheduler worker_2 worker backend
  docker compose stop -t 30 redis db
  Write-Output "Graceful shutdown complete. Volumes and databases were preserved."
} finally {
  Pop-Location
}
