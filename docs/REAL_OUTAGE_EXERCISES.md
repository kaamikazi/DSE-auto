# Real Outage Exercises

## Corrected-B2 continuation — 2026-07-15

The previously blocked exercises were run only after corrected B2 passed. Worker kill/stale-lease recovery, dead-letter creation/replay, real retry/backoff, and PostgreSQL restart during a retry-safe task all passed with real containers and durable PostgreSQL/Redis state. Result hashes are:

- worker loss/stale lease: `4B44C0BEDC92151A44309CDBD7561F2CA756DE01F47478894B5B8F90F3F94939`
- dead-letter/retry: `2D76CA916BB3CD1490BEBC14D21CBC4F8D6A9B6441562DD2984B68AEF072FFB7`
- PostgreSQL restart: `63B9BFC35BCEC0ECB3BB548F28BE49D9D5310D9944FE52F7F00BD5AD87C3D687`

No exercise changed paper orders or fills, produced an OOM, or invalidated reconciliation/audit. These are single-host outage-recovery results, not high-availability, real-market, or live-trading evidence.

Status: **real serialized sub-stage verification**, not simultaneous all-process verification and not real-market evidence.

| Exercise | Result | Evidence |
| --- | --- | --- |
| API stop/start | PASS | Incident `42399d86-2cdb-40e8-a1c3-4e26fc3eaa04`; API health restored; reconciliation/audit valid |
| Abrupt scheduler kill/restart | PASS | Incident `09af655e-8019-4c58-836c-9597e3b262cc`; scheduler restored; reconciliation/audit valid |
| Redis restart with queued task | PASS | Incident `05062c9e-8e50-47b9-a12f-301b118025aa`; unique task succeeded once with one attempt and cleared lease |
| Worker killed during task | BLOCKED / NOT RUN | B2 failed its measured gate and no safe long-running task fixture exists |
| PostgreSQL restart during retry-safe task | BLOCKED / NOT RUN | No safe long-running retry fixture; no result fabricated |
| Dead-letter replay | BLOCKED / NOT RUN | Multi-worker B2 did not qualify |
| Stale-lease recovery after abrupt worker loss | BLOCKED / NOT RUN | Multi-worker B2 did not qualify |

Each executed exercise opened an operational incident before injection, created canonical audit events, recorded exact processes and before/during/after memory, reconciled the paper account, and revalidated the audit chain before resolution. Reports are local ignored artifacts under `reports/incidents/`.
