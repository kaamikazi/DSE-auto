param(
  [Parameter(Mandatory=$true)][string]$Bundle,
  [Parameter(Mandatory=$true)][string]$Destination
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$destinationPath = [System.IO.Path]::GetFullPath($Destination)
if (Test-Path -LiteralPath $destinationPath) {
  if ((Get-ChildItem -LiteralPath $destinationPath -Force | Measure-Object).Count -gt 0) {
    throw "Restore destination must be an existing empty directory or a new directory"
  }
} else {
  New-Item -ItemType Directory -Path $destinationPath | Out-Null
}
$env:PYTHONPATH = Join-Path $root "backend"
& backend\.venv\Scripts\python.exe -c "from pathlib import Path; import json; from app.services.recovery_bundle import verify_recovery_bundle; result=verify_recovery_bundle(Path(r'$Bundle'), Path(r'$destinationPath')); print(json.dumps(result, indent=2)); raise SystemExit(0 if result['passed'] else 2)"
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
Write-Output "Recovery bundle restored and verified in isolated path: $destinationPath"
