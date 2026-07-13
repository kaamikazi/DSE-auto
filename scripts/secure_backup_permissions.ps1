param(
  [Parameter(Mandatory = $true)][string]$Path
)
$ErrorActionPreference = "Stop"
$resolved = (Resolve-Path -LiteralPath $Path).Path
$user = "$env:USERDOMAIN\$env:USERNAME"
icacls $resolved /inheritance:r | Out-Null
icacls $resolved /grant:r "${user}:(F)" "BUILTIN\Administrators:(F)" "NT AUTHORITY\SYSTEM:(F)" | Out-Null
icacls $resolved
