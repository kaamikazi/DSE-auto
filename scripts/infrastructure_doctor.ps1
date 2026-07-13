param(
  [string]$OutputDirectory = "reports\infrastructure"
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$output = Join-Path $root $OutputDirectory
Push-Location $root
try {
  & backend\.venv\Scripts\python.exe scripts\operator.py infrastructure doctor --output-dir $output
  if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
} finally {
  Pop-Location
}
