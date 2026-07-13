$Root = Split-Path -Parent $PSScriptRoot
Start-Process -WindowStyle Hidden -FilePath "$Root\backend\.venv\Scripts\python.exe" -ArgumentList "-m", "uvicorn", "app.main:app", "--reload", "--host", "127.0.0.1", "--port", "8000" -WorkingDirectory "$Root\backend"
Start-Process -WindowStyle Hidden -FilePath "npm.cmd" -ArgumentList "run", "dev", "--", "--hostname", "127.0.0.1" -WorkingDirectory "$Root\frontend"
Write-Host "Backend: http://localhost:8000/api/docs"
Write-Host "Dashboard: http://localhost:3000"
