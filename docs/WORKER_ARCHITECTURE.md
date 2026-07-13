# Worker Architecture

Production-like operation uses separate API, scheduler, and worker processes. The database is the source of truth; Redis is a delivery/wakeup transport, not the authoritative task record.

1. The external scheduler calculates a deterministic time-slot idempotency key.
2. It commits a `task_records` row, then publishes the task ID to Redis.
3. A worker acquires the database lease. PostgreSQL uses `FOR UPDATE SKIP LOCKED` for distributed overlap prevention.
4. The handler completes once, or the task becomes `retry` with bounded backoff.
5. Exhausted tasks become `dead_letter`; they are never silently discarded.
6. Worker heartbeats and lease expiry recover tasks after crashes. Startup requeues durable ready tasks after Redis loss.

Supported tasks are market-data ingestion, campaign scans, signal generation, proposal expiry, reconciliation, EOD processing, evidence generation, backups, audit verification, incident notifications, and the test-only simulation-day handler.

`python -m app.worker_process` and `python -m app.scheduler_process` install graceful SIGINT/SIGTERM handlers. `SCHEDULER_MODE=external` with `SCHEDULER_ENABLED=false` is mandatory in production. In-process APScheduler remains a development-only compatibility mode.

Redis delivery may be duplicated or lost during a restart; database idempotency and ready-task requeue are the recovery boundary. A handler must itself use idempotent business keys. The dashboard shows workers, scheduler heartbeat, queue states, retries, leases, and dead letters.
