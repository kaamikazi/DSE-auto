# backup.ps1
# Creates a timestamped backup copy of the SQLite database

$Timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
$BackupDir = Join-Path $PSScriptRoot "..\data\backups"

if (!(Test-Path $BackupDir)) {
    New-Item -ItemType Directory -Path $BackupDir | Out-Null
}

$SourceDb = Join-Path $PSScriptRoot "..\backend\data\dse_autotrader.db"
$DestDb = Join-Path $BackupDir "dse_autotrader_backup_$Timestamp.db"

if (Test-Path $SourceDb) {
    Copy-Item $SourceDb $DestDb
    Write-Host "Database backup created successfully: $DestDb" -ForegroundColor Green
} else {
    Write-Warning "Source database file not found at: $SourceDb"
}
