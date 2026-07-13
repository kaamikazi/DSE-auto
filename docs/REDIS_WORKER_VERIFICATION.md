# Redis, Worker, and Scheduler Verification

The production-like topology has a Redis broker, an external scheduler, two worker containers, and a separate API. Durable `task_records` remain in PostgreSQL; Redis carries delivery notifications. Idempotency keys, leases, bounded retries, dead-letter states, and stale-worker recovery are database-backed.

Run `scripts\verify_distributed_10_day.ps1` only after the infrastructure doctor passes. It verifies real PostgreSQL/Redis integration, two workers, duplicate delivery protection, worker and scheduler termination/restart, Redis and PostgreSQL restart, a dead-letter task, final reconciliation, and a 10-day three-symbol/two-strategy accelerated campaign.

The resulting campaign is infrastructure validation only. It is synthetic, not real-market evidence, and cannot count toward real-market qualification.

On 2026-07-13 Redis and the multi-process suite remain blocked by the stopped Docker engine. Existing in-process tests are useful unit evidence but are not reported as distributed verification.
