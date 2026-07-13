$ErrorActionPreference = "Stop"
$envPath = Join-Path (Split-Path -Parent $PSScriptRoot) ".env"
if (-not (Test-Path -LiteralPath $envPath)) {
  Write-Output ".env not present; nothing to inspect."
  exit 0
}
$acl = Get-Acl -LiteralPath $envPath
$unsafe = $acl.Access | Where-Object {
  $_.IdentityReference -match "Everyone|BUILTIN\\Users|Authenticated Users" -and
  $_.FileSystemRights.ToString() -match "Write|Modify|FullControl"
}
if ($unsafe) {
  $unsafe | Format-Table IdentityReference, FileSystemRights, AccessControlType
  throw ".env grants unsafe write access"
}
$acl | Format-List Owner, AccessToString
