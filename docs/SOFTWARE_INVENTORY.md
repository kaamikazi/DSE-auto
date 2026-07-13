# Software Inventory

Operational backend direct pins are FastAPI 0.139.0, Uvicorn 0.51.0, pydantic-settings 2.14.2, SQLAlchemy 2.0.51, Alembic 1.18.5, pandas 2.3.3, NumPy 2.5.1, HTTPX 0.28.1, python-multipart 0.0.32, APScheduler 3.11.3, psycopg 3.3.4, and redis-py 5.3.1. Provider integration pins bdshare 1.2.1. Development/test pins are Ruff 0.15.21, mypy 1.20.2, pytest 8.4.2, and pytest-cov 6.3.0.

Frontend direct pins are Next.js 15.5.20, React/React DOM 19.1.1, Recharts 3.9.2, TypeScript 5.8.3, ESLint 9.28.0, Tailwind 3.4.17, and the exact versions in `frontend/package.json`; transitive integrity is in `package-lock.json`.

Machine-generated full transitive inventories, CycloneDX SBOMs, declared license metadata, audits, and SHA-256 lock hashes are produced under `reports/dependencies/`. These reports are local evidence and intentionally ignored by Git. Missing package license metadata must be reviewed against the upstream distribution before redistribution; absence in metadata is not a license conclusion.

The 2026-07-13 generation found zero known vulnerabilities in the runtime backend lock and zero npm audit findings. Vulnerability data ages immediately and must be regenerated for releases.
