param(
  [string]$OutputDirectory = "reports\memory"
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$output = Join-Path $root $OutputDirectory
New-Item -ItemType Directory -Path $output -Force | Out-Null
$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$rawPath = Join-Path $output "memory_raw_$stamp.json"
$jsonPath = Join-Path $output "memory_doctor_$stamp.json"
$markdownPath = Join-Path $output "memory_doctor_$stamp.md"

$os = Get-CimInstance Win32_OperatingSystem
$memory = Get-CimInstance Win32_PerfFormattedData_PerfOS_Memory
$pagefiles = @(Get-CimInstance Win32_PageFileUsage)
$processes = @(Get-Process -ErrorAction SilentlyContinue | ForEach-Object {
  [pscustomobject]@{
    name = $_.ProcessName
    pid = $_.Id
    working_set_gib = [math]::Round($_.WorkingSet64 / 1GB, 3)
    private_gib = [math]::Round($_.PrivateMemorySize64 / 1GB, 3)
  }
})
$topWorking = @($processes | Sort-Object working_set_gib -Descending | Select-Object -First 30)
$topPrivate = @($processes | Sort-Object private_gib -Descending | Select-Object -First 30)

function Measure-ProcessGroup([string[]]$Names) {
  $matching = @($processes | Where-Object { $Names -contains $_.name })
  return [pscustomobject]@{
    process_count = $matching.Count
    working_set_gib = [math]::Round((($matching | Measure-Object working_set_gib -Sum).Sum), 3)
    private_gib = [math]::Round((($matching | Measure-Object private_gib -Sum).Sum), 3)
  }
}

$dockerStats = @()
$dockerOutput = & docker stats --no-stream --format json 2>$null
if ($LASTEXITCODE -eq 0) {
  $dockerStats = @($dockerOutput | ForEach-Object { $_ | ConvertFrom-Json })
}
$wsl = ((& wsl.exe --list --verbose 2>$null) -join "`n") -replace "`0", ""
$compression = Get-Process -Name "Memory Compression" -ErrorAction SilentlyContinue
$allocatedPagefile = (($pagefiles | Measure-Object AllocatedBaseSize -Sum).Sum) / 1024
$usedPagefile = (($pagefiles | Measure-Object CurrentUsage -Sum).Sum) / 1024
$committedGiB = [math]::Round($memory.CommittedBytes / 1GB, 2)
$commitLimitGiB = [math]::Round($memory.CommitLimit / 1GB, 2)

$report = [ordered]@{
  generated_at = (Get-Date).ToUniversalTime().ToString("o")
  read_only = $true
  windows_uptime_hours = [math]::Round(((Get-Date) - $os.LastBootUpTime).TotalHours, 1)
  physical_memory = [ordered]@{
    total_gib = [math]::Round($os.TotalVisibleMemorySize / 1MB, 2)
    available_gib = [math]::Round($memory.AvailableBytes / 1GB, 2)
    cache_gib = [math]::Round($memory.CacheBytes / 1GB, 2)
    paged_pool_gib = [math]::Round($memory.PoolPagedBytes / 1GB, 2)
    nonpaged_pool_gib = [math]::Round($memory.PoolNonpagedBytes / 1GB, 2)
    standby_cache_gib = [math]::Round(($memory.StandbyCacheCoreBytes + $memory.StandbyCacheNormalPriorityBytes + $memory.StandbyCacheReserveBytes) / 1GB, 2)
    compression_working_set_gib = if ($compression) { [math]::Round($compression.WorkingSet64 / 1GB, 3) } else { 0 }
  }
  commit_memory = [ordered]@{
    committed_gib = $committedGiB
    limit_gib = $commitLimitGiB
    headroom_gib = [math]::Round($commitLimitGiB - $committedGiB, 2)
  }
  pagefile = [ordered]@{
    allocated_gib = [math]::Round($allocatedPagefile, 2)
    used_gib = [math]::Round($usedPagefile, 2)
    files = @($pagefiles | ForEach-Object {
      [pscustomobject]@{
        name = $_.Name
        allocated_gib = [math]::Round($_.AllocatedBaseSize / 1024, 2)
        used_gib = [math]::Round($_.CurrentUsage / 1024, 2)
        peak_used_gib = [math]::Round($_.PeakUsage / 1024, 2)
        temporary = $_.TempPageFile
      }
    })
  }
  process_groups = [ordered]@{
    chrome = Measure-ProcessGroup @("chrome")
    docker_desktop = Measure-ProcessGroup @("Docker Desktop")
    com_docker_backend = Measure-ProcessGroup @("com.docker.backend")
    vmmem = Measure-ProcessGroup @("vmmem", "vmmemWSL")
  }
  top_by_working_set = $topWorking
  top_by_private_bytes = $topPrivate
  wsl_distributions = $wsl
  docker_container_stats = $dockerStats
}
$report | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $rawPath -Encoding utf8

Push-Location $root
try {
  $env:PYTHONPATH = Join-Path $root "backend"
  & backend\.venv\Scripts\python.exe scripts\memory_preflight.py --input $rawPath --json-output $jsonPath --markdown-output $markdownPath
  if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
} finally {
  Pop-Location
}
Write-Output "Memory diagnostic JSON: $jsonPath"
Write-Output "Memory diagnostic Markdown: $markdownPath"
