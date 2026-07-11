param([switch]$SkipFrontend)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
if (-not (Test-Path ".env")) { Copy-Item ".env.example" ".env" }
py -3.12 -m venv backend\.venv
& backend\.venv\Scripts\python.exe -m pip install --upgrade pip
& backend\.venv\Scripts\python.exe -m pip install -e "backend[dev]"
Push-Location backend
& .\.venv\Scripts\alembic.exe upgrade head
Pop-Location
if (-not $SkipFrontend) { Push-Location frontend; npm.cmd install; Pop-Location }
Write-Host "Setup complete. Edit .env, then use scripts\start.ps1" -ForegroundColor Green

