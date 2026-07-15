# Distributed Worker Verification

## Corrected B2 and blocked-work rerun — 2026-07-15

The corrected B2 gate passed with the exact two-worker topology and metrics recorded in `PAGING_MEASUREMENT_AUDIT.md`. The now-authorized exercises then passed against real PostgreSQL and Redis:

| Exercise | Result | Durable behavior |
| --- | --- | --- |
| Multi-worker competition | PASS | The same task ID was delivered twice; one worker completed it, the other claimed nothing, attempts remained one, and one outbox effect was produced. |
| Worker killed mid-task | PASS | Attempt 1 lost its worker; the real stale threshold elapsed; stale worker/lease recovery retried and attempt 2 succeeded. |
| Dead-letter and replay | PASS | An intentional failure dead-lettered at attempt 1; one operator replay succeeded at attempt 2. |
| Retry path | PASS | A retry-safe task entered retry at attempt 1 and succeeded after its real backoff at attempt 2. |
| PostgreSQL restart mid-task | PASS | The database was unavailable for 25 seconds; the first worker exited without OOM; stale recovery completed attempt 2 after PostgreSQL health returned. |

Each passing exercise opened/resolved incidents, recorded process and memory evidence, preserved five paper orders and two fills without duplication, reconciled cash, and verified the canonical audit chain. Two earlier competition harness attempts were aborted before a valid competition result (one cleanup defect and one wrong-database worker environment); their evidence is preserved and is not counted as a product failure or pass.

Classification: **blocked after real Stage B startup measurement**.

The production backend image built and the API, scheduler, worker 1, and worker 2 containers started as separate processes with PostgreSQL and Redis. Available physical memory then fell from 3.33 GiB to 2.38 GiB, below the 3 GiB distributed-runtime requirement. The stage failed closed and all four application processes were gracefully stopped; PostgreSQL, Redis, and volumes were preserved.

No worker lease, heartbeat, crash, scheduler restart, Redis restart, PostgreSQL restart, API restart, outbox replay, dead-letter replay, or distributed campaign result is claimed.

## 2026-07-15 continuation

B1 provided real serialized evidence for task creation, one worker claim, one attempt, lease release, heartbeat, API response, and graceful shutdown. B3 provided real API restart, abrupt scheduler restart, and Redis restart with one queued task completing exactly once. B2 failed its memory/paging gate, so multi-worker competition, abrupt worker loss, stale lease recovery, PostgreSQL-mid-task recovery, dead-letter replay, and the distributed campaign remain blocked/not run. The earlier blanket “no restart result” statement above is retained as historical Milestone 9 pre-continuation status.

Paging-audit addendum: the B2 gate result above is retained as historical execution evidence but its paging conclusion is invalid. One startup sample dominated an ambiguous `PageReadsPersec + PageWritesPersec` mean without operational degradation. B2 remains blocked and must be rerun under the corrected multi-signal measurement; no distributed behavior result changes until that happens.
