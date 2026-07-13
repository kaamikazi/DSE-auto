# Infrastructure Incident Tests

`backend/app/services/infrastructure_incidents.py` defines controlled fail-closed exercises for PostgreSQL/Redis loss, one/all worker loss, scheduler/API restart, store restarts, pool exhaustion, disk/backup failure, dead-letter accumulation, stale leases, and corrupt payloads. Every exercise opens an operational incident, links audit evidence, records paper-only safety, and either resolves after revalidation or remains open for explicit operator action.

Run offline evidence with `backend\.venv\Scripts\python.exe scripts\run_infrastructure_incidents.py all`. Reports under `reports/incidents/` are labeled `offline_controlled_simulation`; they are not real process-outage evidence.

Real worker/scheduler/Redis/PostgreSQL restart evidence comes only from `scripts\verify_distributed_10_day.ps1` while Docker services are healthy. A failed exercise leaves paper operations paused; preserve logs and audit events, reconcile, verify backups/migrations, then resolve through the incident workflow.
