# Paging Measurement Audit

Status: **measurement audit completed; B2 remains blocked pending a new run**. No worker, campaign, or outage exercise was run during this audit.

## Original measurement

`scripts/observe_low_memory_runtime.ps1` read `Win32_PerfFormattedData_PerfOS_Memory` with `Get-CimInstance` and stored:

```text
hard_paging_per_second = PageReadsPersec + PageWritesPersec
```

The name was misleading. `Memory\Page Reads/sec` counts disk read operations used to resolve hard faults, not pages, total faults, or pagefile-only reads. A hard fault can load an executable, DLL, memory-mapped file, or pagefile-backed data. `Memory\Page Writes/sec` counts write operations used to free working-set space. Their sum is a project-defined mixture of operations and is not a Windows “hard paging” counter.

Microsoft explicitly warns that hard-fault counters include but are not limited to pagefile reads and that high paging counters do not by themselves prove a RAM shortage. Pagefile pressure must be correlated with available memory, pagefile activity, and operational impact:

- <https://learn.microsoft.com/en-us/troubleshoot/windows-client/performance/how-to-determine-the-appropriate-page-file-size-for-64-bit-versions-of-windows>
- <https://learn.microsoft.com/en-us/troubleshoot/windows-server/performance/ram-virtual-memory-pagefile-management>

## Why the preserved B2 rerun failed

The evaluator averaged all 16 samples and required the mean to be at most 10. The preserved values were `2922, 0, 0, 0, 0, 0, 0, 0, 19, 15, 0, 0, 0, 0, 0, 0`; `(2922 + 19 + 15) / 16 = 184.75`. The 2,922 sample was captured at elapsed second 10 during startup. Although the separate consecutive-spike rule allowed one severe sample, the untrimmed mean independently forced failure.

That result did not establish pagefile thrashing: available RAM never fell below 4.138 GiB and ended 0.683 GiB higher, commit headroom stayed near 20 GiB, pagefile allocation in use fell 0.056 GiB, and no process, database, or audit consequence occurred. The old B2 decision is therefore measurement-invalid. It is not converted to a pass; B2 remains blocked until it is rerun with the corrected diagnostic.

## Corrected read-only diagnostic

Runtime sampling now uses cooked `Get-Counter` values and records these signals independently:

| Counter or signal | Meaning |
| --- | --- |
| `Memory\Page Faults/sec` | All soft and hard faults; diagnostic only |
| `Memory\Pages Input/sec` | Pages read from disk to resolve hard faults; includes file-backed and pagefile-backed data |
| `Memory\Page Reads/sec` | Disk read operations used to resolve hard faults |
| `Paging File(_Total)\% Usage` | Allocated pagefile usage percentage |
| `PhysicalDisk(_Total)\Avg. Disk sec/Read` | Cooked disk read latency, converted to milliseconds |
| `PhysicalDisk(_Total)\Avg. Disk sec/Write` | Cooked disk write latency, converted to milliseconds |
| `PhysicalDisk(_Total)\Avg. Disk Queue Length` | Average in-flight disk requests |
| `PhysicalDisk(_Total)\Current Disk Queue Length` | Current in-flight disk requests |
| `Memory\Available Bytes` via the formatted memory class | Available physical memory |
| `CommitLimit - CommittedBytes` via the formatted memory class | Commit headroom |
| Scheduler heartbeat age | Read-only age of the active scheduler record |
| Worker heartbeat delay | Read-only maximum age of active worker records |

The first 120 seconds are tagged `startup_warmup` and excluded from the decision. At least 600 subsequent seconds are tagged `steady_state` and evaluated. `-Mode Shutdown` records `shutdown_activity` separately without stopping a process; shutdown samples are evidence only and never enter the steady-state decision.

## Multi-signal rule

Sustained paging means at least two consecutive steady-state samples where either `Pages Input/sec > 500` or `Page Reads/sec > 5`. This becomes a paging failure only when at least one consequence is also present:

- available RAM breaches the adaptive reserve or declines beyond the existing limits;
- pagefile use exceeds 50 percent or grows by more than 0.25 GiB;
- disk read latency exceeds 50 ms or average queue length exceeds 2 for at least two consecutive samples;
- scheduler heartbeat lag exceeds 10 seconds;
- worker heartbeat delay exceeds 60 seconds;
- a process restarts, disappears, or reports OOM;
- PostgreSQL becomes unhealthy.

A paging spike or sustained hard-fault reads without a consequence are classified `warning`, not `fail`. Independent safety failures—low commit headroom, falling/critically low memory, pagefile growth, scheduler or worker unresponsiveness, restart/OOM/process loss, database failure, audit failure, incomplete measurement, or an observation shorter than ten steady-state minutes—still fail closed.
