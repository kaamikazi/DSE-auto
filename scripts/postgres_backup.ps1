param(
  [Parameter(Mandatory = $true)][string]$OutputPath,
  [ValidatePattern('^[a-zA-Z0-9_]+$')][string]$Database = 'dse_autotrader'
)
$ErrorActionPreference = "Stop"
$parent = Split-Path -Parent $OutputPath
if ($parent) { New-Item -ItemType Directory -Force -Path $parent | Out-Null }
$containerBackup = "/tmp/dse-autotrader-backup-$([guid]::NewGuid().ToString('N')).dump"
try {
  docker compose exec -T db pg_dump --format=custom --no-owner --username=dse `
    --file=$containerBackup $Database
  if ($LASTEXITCODE -ne 0) { throw "PostgreSQL backup failed" }
  docker compose cp "db:$containerBackup" $OutputPath
  if ($LASTEXITCODE -ne 0) { throw "Could not copy PostgreSQL backup from the db container" }
} finally {
  docker compose exec -T db rm -f $containerBackup | Out-Null
}
if (-not (Test-Path -LiteralPath $OutputPath) -or (Get-Item -LiteralPath $OutputPath).Length -lt 5) {
  throw "PostgreSQL backup is empty or truncated"
}
$header = [System.IO.File]::ReadAllBytes((Resolve-Path -LiteralPath $OutputPath))[0..4]
if ([System.Text.Encoding]::ASCII.GetString($header) -ne 'PGDMP') {
  throw "PostgreSQL backup does not have a valid custom-format header"
}
Get-FileHash -Algorithm SHA256 -LiteralPath $OutputPath
$user = "$env:USERDOMAIN\$env:USERNAME"
icacls $OutputPath /inheritance:r | Out-Null
icacls $OutputPath /grant:r "${user}:(F)" "BUILTIN\Administrators:(F)" "NT AUTHORITY\SYSTEM:(F)" | Out-Null
Write-Output "Backup ACL restricted to the operator, Administrators, and SYSTEM."
