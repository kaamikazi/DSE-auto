# Dependency Locking

Backend direct dependencies are exact-pinned in `backend/pyproject.toml`. Input groups and hash locks live under `backend/requirements/`:

- `runtime`: production API, data, scheduler, PostgreSQL, and Redis dependencies
- `development`: runtime plus Ruff and strict mypy
- `testing`: development plus pytest/coverage
- `providers`: runtime plus bdshare

Regenerate a lock from `backend/requirements` with pinned `pip-tools==7.5.3`, for example `..\.venv\Scripts\pip-compile.exe --generate-hashes --strip-extras --output-file runtime.lock.txt runtime.in`. Review the complete diff and audits before committing. Install with `python -m pip install --require-hashes -r testing.lock.txt`.

The optional `quantstats>=0.0.64` reports extra is intentionally not part of the operational locks: it is platform-sensitive, unused by the paper runtime, and must be evaluated/pinned in a separate research environment before use.

Frontend uses `npm ci` and committed `package-lock.json`. The only override is `postcss=8.5.10`. Run `scripts/generate_dependency_evidence.ps1` for inventories, backend/frontend audits, CycloneDX SBOMs, Python license metadata, and lock hashes. Upgrade one group at a time, regenerate hashes, fresh-install, audit, test, and commit the input plus lock together.
