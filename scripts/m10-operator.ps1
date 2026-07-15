param(
  [Parameter(ValueFromRemainingArguments = $true)]
  [string[]]$Arguments
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Push-Location $root
try {
  & backend\.venv\Scripts\python.exe scripts\real_market_operator.py @Arguments
  if ($LASTEXITCODE -ne 0) { throw "Milestone 10 operator command failed closed" }
} finally {
  Pop-Location
}
