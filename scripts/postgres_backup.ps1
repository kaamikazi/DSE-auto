param(
  [Parameter(Mandatory = $true)][string]$OutputPath
)
$ErrorActionPreference = "Stop"
$parent = Split-Path -Parent $OutputPath
if ($parent) { New-Item -ItemType Directory -Force -Path $parent | Out-Null }
docker compose exec -T db pg_dump --format=custom --no-owner --username=dse dse_autotrader > $OutputPath
if (-not (Test-Path -LiteralPath $OutputPath) -or (Get-Item -LiteralPath $OutputPath).Length -eq 0) {
  throw "PostgreSQL backup is empty"
}
Get-FileHash -Algorithm SHA256 -LiteralPath $OutputPath
$user = "$env:USERDOMAIN\$env:USERNAME"
icacls $OutputPath /inheritance:r | Out-Null
icacls $OutputPath /grant:r "${user}:(F)" "BUILTIN\Administrators:(F)" "NT AUTHORITY\SYSTEM:(F)" | Out-Null
Write-Output "Backup ACL restricted to the operator, Administrators, and SYSTEM."
