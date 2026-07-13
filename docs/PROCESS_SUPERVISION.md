# Process Supervision

Startup order is PostgreSQL and Redis, then API, scheduler, worker 1, worker 2, and frontend. Compose health/dependency conditions prevent the application tier from starting before its stores are healthy. Every application process uses `restart: unless-stopped`.

`scripts/start_production_like.ps1` runs the doctor, starts services in order, and captures startup logs/state. `scripts/status_production_like.ps1` reports service name, container ID, host process ID, state, health, and publishers. `scripts/stop_production_like.ps1` saves logs and gracefully stops frontend/scheduler/workers/API before Redis/PostgreSQL while preserving volumes.

Logs are under `logs/production-like/`; current status is under `data/process-state/`. These are local runtime artifacts and are excluded from Git and recovery source content.

After an abrupt stop: keep paper trading paused, capture logs, run status, verify migrations, recover stale leases/workers, verify audit, reconcile, verify the latest backup, then obtain operator acknowledgement before resuming.
