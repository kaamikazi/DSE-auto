# Disaster Recovery

Recovery preserves the database, Redis-recoverable task/outbox state, canonical audit plus legacy archive, evidence packs, and non-secret configuration. Secrets must be backed up separately using an approved encrypted secret manager; never include `.env` in an evidence bundle.

## SQLite exercise

The service uses SQLite's online backup API, restores into an isolated database, compares logical hashes, runs `PRAGMA quick_check`, counts tables, verifies the audit chain, copies evidence/configuration, and records measured RPO/RTO plus a hashed report. Milestone 7 passed with 33 restored tables, RPO approximately 0.007 seconds, RTO approximately 0.138 seconds, valid audit evidence, and excluded secrets.

## PostgreSQL and clean-machine recovery

1. Install the pinned application revision, Python 3.12, Node, Docker/PostgreSQL 16, and Redis 7.
2. Restore encrypted secrets through the secret manager and restrict ACLs.
3. Restore `pg_dump` into a new database with `scripts\postgres_restore.ps1`.
4. Run Alembic preflight, table counts/hashes, audit verification, reconciliation, and evidence hash checks.
5. Start Redis empty; workers requeue durable ready tasks/outbox records from PostgreSQL.
6. Start one scheduler and workers, validate heartbeats, then start API/frontend on localhost.
7. Keep paper trading paused until an operator acknowledges the recovery evidence.

`scripts\secure_backup_permissions.ps1` restricts a backup to the operator, Administrators, and SYSTEM. PostgreSQL restore was not executed in Milestone 7 because Docker was unavailable; this remains a release blocker.
