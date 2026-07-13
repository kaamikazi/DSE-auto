$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$stateDir = Join-Path $root "data\process-state"
New-Item -ItemType Directory -Force -Path $stateDir | Out-Null
Push-Location $root
try {
  $raw = docker compose --profile production-like ps --format json 2>&1
  $exitCode = $LASTEXITCODE
  $containers = @()
  if ($exitCode -eq 0) {
    foreach ($line in $raw) {
      if ($line.Trim()) { $containers += ($line | ConvertFrom-Json) }
    }
  }
  $status = [ordered]@{
    generated_at = (Get-Date).ToUniversalTime().ToString("o")
    docker_available = ($exitCode -eq 0)
    paper_trading = $true
    live_trading_enabled = $false
    containers = @($containers | ForEach-Object {
      $processId = $null
      if ($_.ID) { $processId = docker inspect --format '{{.State.Pid}}' $_.ID 2>$null }
      [ordered]@{service=$_.Service; name=$_.Name; state=$_.State; health=$_.Health; container_id=$_.ID; process_id=$processId; publishers=$_.Publishers}
    })
  }
  $status | ConvertTo-Json -Depth 8 | Set-Content -Encoding utf8 (Join-Path $stateDir "compose-status.json")
  $status | ConvertTo-Json -Depth 8
  if ($exitCode -ne 0) { exit 2 }
} finally {
  Pop-Location
}
