# Docker Operations

The `production-like` Compose profile runs PostgreSQL, Redis, API, frontend, an external scheduler, and two independent workers. PostgreSQL and Redis use named persistent volumes; the isolated `db_test` service uses tmpfs. Host bindings are loopback-only.

Required `.env` values include strong `POSTGRES_PASSWORD`, a distinct `POSTGRES_TEST_PASSWORD`, strong distinct API/reviewer keys, `TRADING_MODE=paper`, `LIVE_TRADING_ENABLED=false`, and `BROKER_ADAPTER=disabled`.

Commands:

- Start in dependency order: `scripts\start_production_like.ps1`
- Inspect container health, IDs and process IDs: `scripts\status_production_like.ps1`
- Gracefully stop applications, Redis, then PostgreSQL: `scripts\stop_production_like.ps1`
- Full real-process exercise: `scripts\verify_distributed_10_day.ps1`

The stop script preserves volumes. Never use `docker compose down -v` during incident response or routine shutdown. Capture `docker compose --profile production-like logs --no-color` before recovery. If a restart loop occurs, leave paper operations paused, preserve logs, verify database/audit state, and require operator acknowledgement before resuming.
