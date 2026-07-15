# Paging Measurement Audit

## Corrected B2 rerun — 2026-07-15

The corrected rerun passed after a 120-second warm-up and 612.4 seconds of steady-state observation. The exact topology was PostgreSQL, Redis, one scheduler, and two workers; API, `db_test`, frontend, reloaders, and watchers were absent.

- Available memory started at 5.316 GiB, ended at 5.563 GiB, and never fell below 5.187 GiB.
- Commit headroom never fell below 20.628 GiB; pagefile growth was 0.007 GiB.
- `Page Faults/sec` was high (121,374.19 average; 189,142.35 peak), but `Pages Input/sec` and `Page Reads/sec` averaged 0.44 and peaked at 2. Sustained hard-fault reads were false.
- Disk-read latency peaked at 0.56 ms, queue length at 0.01, scheduler lag at 0.958 seconds, and worker-heartbeat delay at 28.262 seconds.
- No restart, OOM, task loss, database failure, or audit failure occurred.

This is the intended multi-signal outcome: high aggregate faults without paging pressure or operational degradation are not thrashing. Raw SHA-256: `CCA5C08999BDD43A2A985AE21AF3A59D44371E670C315C5F07D7131E12462BB2`; result SHA-256: `20E85EAA2284F7B417171D82FC878FD7942DAF221FA59F7E5D58D0EFEAE18A77`. An actual scheduler/two-worker stop was captured separately in four shutdown samples, three of which recorded missing application processes as expected; SHA-256 `DDAC5EF689522B58FA2A108A28A8C43EA3847BD7110A0A0A887B9BE50C7F27D2`.

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
