# SQLite to PostgreSQL Migration

The migration tool is non-destructive: it reads the operational SQLite database and requires an empty destination. It never deletes or rewrites the source.

1. Stop application writers and create/verify a SQLite backup.
2. Upgrade both databases to Alembic `0008`.
3. Run the preflight:

```powershell
backend\.venv\Scripts\python.exe scripts\migrate_sqlite_to_postgresql.py `
  --source sqlite:///./backend/data/dse_autotrader.db `
  --destination postgresql+psycopg://dse:REDACTED@localhost:5432/dse_autotrader
```

4. Review every source table count and SHA-256 logical row hash.
5. On a clean destination, repeat with `--execute`.
6. Require `count_match=true`, `hash_match=true`, and `verified=true` before changing `DATABASE_URL`.
7. Run audit verification, paper-account reconciliation, evidence lookup, and an isolated PostgreSQL backup/restore.

Rows are copied in metadata dependency order. The destination must be empty to prevent merges, duplicate effects, or silent replacement. URLs are redacted in output. The Milestone 7 operational dry run enumerated 33 tables and passed preflight; the real PostgreSQL copy was blocked by the unavailable Docker daemon. The unit migration exercise copied between isolated SQLite databases and verified identical counts/hashes.
