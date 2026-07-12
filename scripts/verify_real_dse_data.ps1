param([string]$Provider = "bdshare", [string]$Symbol = "GP")
$ErrorActionPreference = "Stop"
Push-Location "$PSScriptRoot\..\backend"
try {
  & .\.venv\Scripts\python.exe ..\scripts\operator.py verify-data --provider $Provider --symbol $Symbol
} finally { Pop-Location }
