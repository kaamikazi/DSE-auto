# Distributed Worker Verification

Classification: **blocked after real Stage B startup measurement**.

The production backend image built and the API, scheduler, worker 1, and worker 2 containers started as separate processes with PostgreSQL and Redis. Available physical memory then fell from 3.33 GiB to 2.38 GiB, below the 3 GiB distributed-runtime requirement. The stage failed closed and all four application processes were gracefully stopped; PostgreSQL, Redis, and volumes were preserved.

No worker lease, heartbeat, crash, scheduler restart, Redis restart, PostgreSQL restart, API restart, outbox replay, dead-letter replay, or distributed campaign result is claimed.

## 2026-07-15 continuation

B1 provided real serialized evidence for task creation, one worker claim, one attempt, lease release, heartbeat, API response, and graceful shutdown. B3 provided real API restart, abrupt scheduler restart, and Redis restart with one queued task completing exactly once. B2 failed its memory/paging gate, so multi-worker competition, abrupt worker loss, stale lease recovery, PostgreSQL-mid-task recovery, dead-letter replay, and the distributed campaign remain blocked/not run. The earlier blanket “no restart result” statement above is retained as historical Milestone 9 pre-continuation status.
