# PostgreSQL Verification

Real PostgreSQL evidence requires a healthy Docker engine; SQLite does not qualify. Run:

1. `scripts\verify_postgresql_migrations.ps1` for clean `0009` installation, downgrade to `0007`, and re-upgrade.
2. `scripts\verify_sqlite_to_postgresql.ps1` to create a new isolated destination, migrate without modifying the original SQLite database, and require matching table counts and deterministic hashes.
3. `scripts\verify_distributed_10_day.ps1` for integration tests and restart recovery.

The database layer enables pre-ping, bounded pool timeouts/recycling, transaction retry limits, and SQLSTATE handling for serialization failure (`40001`) and deadlock (`40P01`). Integration tests cover clean migrations and connectivity; the distributed harness restarts PostgreSQL between phases and checks reconnect/reconciliation.

As of 2026-07-13 these real commands are **not run** because the Docker Linux engine is unavailable. No PostgreSQL, SQLite-migration, deadlock, pool-recovery, or restart result is claimed.
