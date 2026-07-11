$Root = Split-Path -Parent $PSScriptRoot
Start-Process -WindowStyle Hidden -FilePath "$Root\backend\.venv\Scripts\python.exe" -ArgumentList "-m", "uvicorn", "app.main:app", "--reload", "--port", "8000" -WorkingDirectory "$Root\backend"
Start-Process -WindowStyle Hidden -FilePath "npm.cmd" -ArgumentList "run", "dev" -WorkingDirectory "$Root\frontend"
Write-Host "Backend: http://localhost:8000/api/docs"
Write-Host "Dashboard: http://localhost:3000"

