# PostgreSQL Operations

PostgreSQL 16 is the production-like database. SQLite remains supported only for local development, recovery drills, and isolated tests. Production startup refuses SQLite and refuses to create tables automatically; Alembic must already be at `0008`.

## Topology and startup

`docker-compose.yml` defines `db`, isolated `db_test`, Redis with AOF, API, worker, scheduler, and frontend services. Published ports bind to `127.0.0.1`. Set distinct strong values for `POSTGRES_PASSWORD`, `POSTGRES_TEST_PASSWORD`, `API_SECRET_KEY`, and `REVIEWER_API_SECRET_KEY`, then run:

```powershell
docker compose up -d db db_test redis
powershell -ExecutionPolicy Bypass -File scripts\verify_postgresql_migrations.ps1
docker compose up -d backend worker scheduler frontend
```

The API reports database dialect, pool state, server version, transaction isolation, read-only state, `pg_is_in_recovery()`, and replication readiness. A healthy primary does not prove a replica or failover mechanism exists.

## Transactions and pooling

The default isolation level is `READ COMMITTED`. Use `REPEATABLE READ` or `SERIALIZABLE` only for a documented invariant. `run_transaction` retries a complete idempotent transaction on PostgreSQL serialization failure (`40001`) and deadlock (`40P01`) with bounded exponential backoff. Never retry only the final statement of a multi-step business action.

Pool settings are `DATABASE_POOL_SIZE`, `DATABASE_MAX_OVERFLOW`, `DATABASE_POOL_TIMEOUT_SECONDS`, and `DATABASE_POOL_RECYCLE_SECONDS`; `pool_pre_ping` is always enabled. Size the combined API and worker pools below the PostgreSQL connection limit.

## Migration and backup gates

- `alembic upgrade head` must succeed before production startup.
- `scripts\verify_postgresql_migrations.ps1` exercises clean upgrade, downgrade to `0007`, and re-upgrade on `db_test`.
- `scripts\postgres_backup.ps1` creates a custom-format `pg_dump`, hashes it, and restricts its Windows ACL.
- `scripts\postgres_restore.ps1` restores into a separate verification database and counts public tables.
- Never test restore over the only operational database.

Milestone 7 did not execute PostgreSQL integration because the Docker Desktop Linux daemon was unavailable. Compose syntax passed; PostgreSQL readiness remains implementation-complete but environment-unverified.
