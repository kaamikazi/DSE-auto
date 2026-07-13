param(
  [string]$OutputDirectory = "reports\dependencies"
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$output = Join-Path $root $OutputDirectory
$python = Join-Path $root "backend\.venv\Scripts\python.exe"
New-Item -ItemType Directory -Force -Path $output | Out-Null

Push-Location $root
try {
  & $python -m pip list --format json | Set-Content -Encoding utf8 (Join-Path $output "backend-inventory.json")
  if ($LASTEXITCODE -ne 0) { throw "Backend inventory failed" }
  & $python -m pip_audit -r backend\requirements\runtime.lock.txt --format json --output (Join-Path $output "backend-audit.json")
  if ($LASTEXITCODE -notin @(0, 1)) { throw "Backend vulnerability audit failed to execute" }
  & $python -m pip_audit -r backend\requirements\runtime.lock.txt --format cyclonedx-json --output (Join-Path $output "backend-sbom.cdx.json")
  if ($LASTEXITCODE -notin @(0, 1)) { throw "Backend SBOM generation failed" }
  & $python scripts\dependency_license_inventory.py --output (Join-Path $output "backend-licenses.json")
  if ($LASTEXITCODE -ne 0) { throw "Backend license inventory failed" }

  Push-Location frontend
  try {
    npm ls --all --json | Set-Content -Encoding utf8 (Join-Path $output "frontend-inventory.json")
    if ($LASTEXITCODE -ne 0) { throw "Frontend inventory failed" }
    npm sbom --sbom-format cyclonedx | Set-Content -Encoding utf8 (Join-Path $output "frontend-sbom.cdx.json")
    if ($LASTEXITCODE -ne 0) { throw "Frontend SBOM generation failed" }
    npm audit --json | Set-Content -Encoding utf8 (Join-Path $output "frontend-audit.json")
    if ($LASTEXITCODE -notin @(0, 1)) { throw "Frontend audit failed to execute" }
  } finally {
    Pop-Location
  }

  Get-FileHash backend\requirements\*.lock.txt, frontend\package-lock.json -Algorithm SHA256 |
    Select-Object Path, Hash | ConvertTo-Json | Set-Content -Encoding utf8 (Join-Path $output "dependency-hashes.json")
  Write-Output "Dependency evidence written to $output"
} finally {
  Pop-Location
}
