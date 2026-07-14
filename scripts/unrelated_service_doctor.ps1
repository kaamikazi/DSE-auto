param([string]$OutputDirectory = "reports\memory")
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$output = Join-Path $root $OutputDirectory
New-Item -ItemType Directory -Force -Path $output | Out-Null
$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$jsonPath = Join-Path $output "unrelated_services_$stamp.json"
$markdownPath = Join-Path $output "unrelated_services_$stamp.md"

$protectedServices = @('com.docker.service','WinDefend')
$databasePatterns = 'Oracle|MySQL|MSSQL|SQLAgent|SQLBrowser|SQLTELEMETRY|SQLWriter'
$databaseServices = @(Get-Service | Where-Object {
  ($_.Name -match $databasePatterns -or $_.DisplayName -match 'Oracle|MySQL|SQL Server') -and
  $protectedServices -notcontains $_.Name
} | ForEach-Object {
  [pscustomobject]@{
    name = $_.Name
    display_name = $_.DisplayName
    status = [string]$_.Status
    optional_command = if ($_.Status -eq 'Running') { "Stop-Service -Name '$($_.Name)'" } else { $null }
    effect = "Stops the unrelated local database service only; its next start remains manual/service-policy controlled."
  }
})

$categories = [ordered]@{
  chrome = @('chrome')
  ide = @('Code','devenv','idea64','pycharm64','webstorm64','rider64')
  discord = @('Discord')
  game_launcher = @('steam','EpicGamesLauncher','Battle.net','RiotClientServices')
  optional_heavy = @('LM Studio','ms-teams','Teams','OneDrive')
}
$optionalProcesses = @()
foreach ($category in $categories.Keys) {
  $matches = @(Get-Process -ErrorAction SilentlyContinue | Where-Object { $categories[$category] -contains $_.ProcessName })
  if ($matches.Count -eq 0) { continue }
  $ids = @($matches.Id | Sort-Object)
  $optionalProcesses += [pscustomobject]@{
    category = $category
    names = @($matches.ProcessName | Sort-Object -Unique)
    process_ids = $ids
    working_set_gib = [math]::Round((($matches | Measure-Object WorkingSet64 -Sum).Sum) / 1GB, 3)
    private_gib = [math]::Round((($matches | Measure-Object PrivateMemorySize64 -Sum).Sum) / 1GB, 3)
    optional_command = "Stop-Process -Id $($ids -join ',')"
    effect = "Closes only the listed optional user processes; unsaved work may be lost."
  }
}

$report = [ordered]@{
  generated_at = (Get-Date).ToUniversalTime().ToString('o')
  read_only = $true
  automatic_stops_performed = $false
  protected = @('Docker Desktop','WSL2','project PostgreSQL container','project Redis container','Windows core services','Microsoft Defender')
  database_services = $databaseServices
  optional_processes = $optionalProcesses
  note = "Commands are suggestions for explicit operator review only. This diagnostic never executes them."
}
$report | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $jsonPath -Encoding utf8
$rows = @(
  '# Unrelated Service Diagnostic', '',
  "Generated: ``$($report.generated_at)``", '',
  '**Read-only:** no service or process was stopped.', '',
  '## Local database services', '',
  '| Status | Service | Effect | Optional operator command |',
  '| --- | --- | --- | --- |'
)
foreach ($item in $databaseServices) {
  $command = if ($item.optional_command) { "``$($item.optional_command)``" } else { 'None (already stopped)' }
  $rows += "| $($item.status) | $($item.name) | $($item.effect) | $command |"
}
$rows += @('', '## Optional user processes', '', '| Category | Working set GiB | Effect | Optional operator command |', '| --- | ---: | --- | --- |')
foreach ($item in $optionalProcesses) {
  $rows += "| $($item.category) | $($item.working_set_gib) | $($item.effect) | ``$($item.optional_command)`` |"
}
$rows += @('', 'Never stop Docker Desktop, WSL2, the project PostgreSQL/Redis containers, Windows core services, or Defender for this verification.')
$rows -join "`n" | Set-Content -LiteralPath $markdownPath -Encoding utf8
Write-Output "Unrelated-service JSON: $jsonPath"
Write-Output "Unrelated-service Markdown: $markdownPath"
