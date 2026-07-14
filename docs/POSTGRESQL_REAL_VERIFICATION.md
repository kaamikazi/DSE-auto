# PostgreSQL Real Verification

Classification: **verified with real infrastructure for Stage A**.

The isolated `db_test` PostgreSQL 16 container on `127.0.0.1:15432` passed a clean Alembic upgrade to `0009`, downgrade to `0008`, re-upgrade to `0009`, and a connectivity query. The test URL was loaded from the local uncommitted `.env`; no password was printed or committed.

The run exposed and fixed Alembic test-URL precedence: programmatic integration runs now use an explicit `database_url_override` rather than the cached SQLite test setting. Operational SQLite was not modified. SQLite-to-PostgreSQL copy comparison, PostgreSQL backup/restore, restart recovery, deadlock/serialization exercises, and pool recovery remain blocked/not run in this continuation.
