$ErrorActionPreference = "Stop"
do {
  Write-Host "`nDSE PAPER OPERATOR (LIVE TRADING DISABLED)"
  Write-Host "1 Status  2 Start  3 Pause  4 Resume  5 Stop  6 Verify data  7 Backup  8 Emergency stop  0 Exit"
  $choice = Read-Host "Select"
  if ($choice -in @("1","2","3","4","5")) {
    $name = Read-Host "Session name"
    $actions = @{"1"="status";"2"="start";"3"="pause";"4"="resume";"5"="stop"}
    Push-Location "$PSScriptRoot\..\backend"
    try { & .\.venv\Scripts\python.exe ..\scripts\operator.py session $actions[$choice] $name } finally { Pop-Location }
  } elseif ($choice -eq "6") { & "$PSScriptRoot\verify_real_dse_data.ps1" }
  elseif ($choice -eq "7") { & "$PSScriptRoot\backup.ps1" }
  elseif ($choice -eq "8") { Write-Warning "Use Telegram /emergency_stop or the authenticated API endpoint." }
} while ($choice -ne "0")
