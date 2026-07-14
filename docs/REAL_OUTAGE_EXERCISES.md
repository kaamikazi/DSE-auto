# Real Outage Exercises

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
