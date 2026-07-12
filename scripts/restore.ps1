# restore.ps1
# Restores the SQLite database from a selected backup file

param (
    [Parameter(Mandatory=$true)]
    [string]$BackupFile
)

$DestDb = Join-Path $PSScriptRoot "..\backend\data\dse_autotrader.db"

if (Test-Path $BackupFile) {
    # Perform safety copy before overwrite
    if (Test-Path $DestDb) {
        $PreRestoreBackup = $DestDb + ".pre_restore"
        Copy-Item $DestDb $PreRestoreBackup -Force
        Write-Host "Created pre-restore database backup at: $PreRestoreBackup" -ForegroundColor Yellow
    }

    Copy-Item $BackupFile $DestDb -Force
    Write-Host "Database restored successfully from $BackupFile to $DestDb" -ForegroundColor Green
} else {
    Write-Error "Backup file not found at: $BackupFile"
}
