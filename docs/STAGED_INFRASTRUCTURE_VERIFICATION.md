# Staged Infrastructure Verification

Use `start_infrastructure_stage.ps1`, `status_infrastructure_stage.ps1`, and `stop_infrastructure_stage.ps1`. Every start takes a new memory snapshot before changing services and reruns the tier-specific doctor afterward.

- Stage A: `db`, `db_test`, Redis, PostgreSQL tests, and Redis tests. Verified with real infrastructure on 2026-07-14.

## Low-memory serialized continuation (2026-07-15)

| Sub-stage | Classification | Result |
| --- | --- | --- |
| B1: db, Redis, API, scheduler, worker 1 | Real serialized | PASS, 610.2-second gate plus task/lease/heartbeat/API/graceful-stop checks |
| B2: db, Redis, scheduler, workers 1+2 | Real serialized | FAIL CLOSED: paging average 136.75/s, 2,178/s peak, 0.333 GiB decline |
| B3: db, Redis, API, scheduler, worker 1 | Real serialized | PASS, 610.0-second gate; API/scheduler/Redis restart exercises passed |

This is not simultaneous API+scheduler+two-worker validation. B2 failure blocks multi-worker and campaign claims.
- Stage B: stops `db_test`, then uses `db`, Redis, API, scheduler, and two workers without the frontend. Real startup was attempted, but post-start available memory fell to 2.38 GiB; the processes were stopped and the stage is blocked.
- Stage C: distributed accelerated campaign using the Stage B production images. Requires the 4 GiB/10 GiB campaign margins and was not run.

The scripts never run the frontend development server, never use `docker compose down`, never delete volumes, and roll back application processes after a failed post-start gate. PAPER mode, live-disabled, and broker-disabled settings remain mandatory.
