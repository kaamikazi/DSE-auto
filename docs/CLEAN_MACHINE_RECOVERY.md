# Clean-Machine Recovery

Create a bundle with `backend\.venv\Scripts\python.exe scripts\create_recovery_bundle.py`. It contains the Git-tracked source revision, an online SQLite backup, audit archives/evidence, operational state counts, migration revision, dependency locks, restore scripts, a manifest, and per-file SHA-256 hashes.

The bundle excludes `.env`, credentials/secrets files, `.git`, venvs, `node_modules`, `.next`, and caches. `.env.example` is preserved. Restore only to a new or empty isolated directory:

`scripts\restore_recovery_bundle.ps1 -Bundle <zip> -Destination <empty-directory>`

Verification rejects unsafe paths, verifies every manifest hash, runs SQLite `quick_check`, confirms campaign/qualification tables and dependency locks, and asserts paper-only safety metadata. Dependency installation is separately proved with `--require-hashes`; frontend is proved with `npm ci`.

Do not overwrite the operational database from a bundle until the isolated restore, audit verification, migration check, evidence-hash review, campaign/qualification review, and operator approval all pass.
