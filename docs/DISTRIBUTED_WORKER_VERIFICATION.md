# Distributed Worker Verification

Classification: **blocked after real Stage B startup measurement**.

The production backend image built and the API, scheduler, worker 1, and worker 2 containers started as separate processes with PostgreSQL and Redis. Available physical memory then fell from 3.33 GiB to 2.38 GiB, below the 3 GiB distributed-runtime requirement. The stage failed closed and all four application processes were gracefully stopped; PostgreSQL, Redis, and volumes were preserved.

No worker lease, heartbeat, crash, scheduler restart, Redis restart, PostgreSQL restart, API restart, outbox replay, dead-letter replay, or distributed campaign result is claimed.
