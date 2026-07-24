# DSE AutoTrader

> **PAPER TRADING ONLY**
>
> **LIVE BROKER EXECUTION IS NOT IMPLEMENTED**
>
> **NO PROFITABILITY GUARANTEE**
>
> **CURRENT PUBLIC DSE PROVIDERS ARE NOT TRUSTED FOR LIVE OPERATION**
>
> **REAL-MARKET QUALIFICATION REMAINS 0/60**

DSE AutoTrader is a Windows-first research, portfolio-monitoring, backtesting, evidence-governance, and supervised paper-trading platform for the Dhaka Stock Exchange. It is designed to fail closed when market data, timestamps, risk state, approvals, or operational health are uncertain.

This project is not financial advice and is not ready for real-money operation.

## Current status

Milestone 11 is complete. The repository includes review-only evidence intake, deterministic extraction, claim review and conflicts, rule and fee decision assistants, portfolio-statement drafts, market-dataset quality review, completeness tracking, and independently scoped approval packs.

No evidence upload or review can activate rules, fees, risk limits, datasets, strategies, campaigns, or trading. No real-market day has qualified toward the required 60-day evidence target.

## Architecture

- **Backend:** Python 3.12, FastAPI, SQLAlchemy, Alembic, Pydantic
- **Frontend:** Next.js, React, TypeScript, Tailwind CSS, Recharts
- **Persistence:** SQLite for local development; PostgreSQL for production-like paper infrastructure
- **Coordination:** Redis-backed external workers and scheduler with leases, heartbeats, retries, recovery, and dead letters
- **Market data:** normalized adapters for mock, CSV, bdshare, and bdfinance contracts
- **Execution:** local paper broker only; the official broker adapter refuses execution
- **Governance:** append-only audit chain, human approvals, evidence hashes, source hierarchy, timestamp trust, and fail-closed readiness gates

## Major features

- Provider freshness, timestamp-trust, disagreement, failover, and circuit-breaker controls
- Deterministic strategies, walk-forward analysis, sensitivity checks, and portfolio/DSEX comparisons
- Versioned signals, pre-trade risk checks, idempotent proposals, and approval revalidation
- Partial paper fills, fees, slippage, liquidity limits, emergency stop, and reconciliation
- PostgreSQL/Redis process topology, durable task queues, outbox events, backups, and restore tooling
- Evidence collection cases, immutable intake, deterministic extraction, claim review, and conflict handling
- Rule/fee assistants, portfolio-statement drafts, dataset-quality reports, completeness tracking, and scoped approval packs
- Operator dashboard with permanent paper-only and live-disabled banners

## Repository structure

```text
backend/        FastAPI application, migrations, locked dependencies, and tests
frontend/       Next.js operator dashboard
config/         Public paper-trading configuration and market-calendar assumptions
data/imports/   Sanitized templates and deterministic public fixtures only
docs/           Architecture, operations, evidence, safety, and limitation guides
scripts/        Setup, verification, backup, recovery, and paper-operations tools
.github/        Public CI and contribution templates
```

Operational databases, imports, evidence, reports, logs, backups, audit archives, and credentials are intentionally excluded from Git.

## Environment setup

Copy the public template and replace every `change-me` placeholder locally:

```powershell
Copy-Item .env.example .env
```

The following invariants must remain:

```dotenv
TRADING_MODE=paper
LIVE_TRADING_ENABLED=false
BROKER_ADAPTER=disabled
```

Never commit `.env`, API secrets, Telegram tokens/chat IDs, broker credentials, account statements, or portfolio records.

## Backend setup

```powershell
Set-Location backend
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install --require-hashes -r requirements\testing.lock.txt
.\.venv\Scripts\python.exe -m pip install --no-deps -e .
.\.venv\Scripts\alembic.exe upgrade head
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload
```

API documentation is available at `http://127.0.0.1:8000/api/docs`.

## Frontend setup

```powershell
Set-Location frontend
npm.cmd ci
npm.cmd run dev
```

The dashboard is available at `http://127.0.0.1:3000`.

## Docker, PostgreSQL, and Redis

Set distinct local PostgreSQL passwords in `.env`, then start the durable stores:

```powershell
docker compose up -d db db_test redis
docker compose ps
```

Start the complete production-like **paper** topology only after the infrastructure doctor passes:

```powershell
.\scripts\infrastructure_doctor.ps1
docker compose --profile production-like up -d
```

Services bind to loopback by default. Production-like infrastructure does not imply live-trading or real-market readiness.

## Migrations

```powershell
Set-Location backend
.\.venv\Scripts\alembic.exe current
.\.venv\Scripts\alembic.exe upgrade head
```

The current schema head is `0011`.

## Verification

Backend:

```powershell
Set-Location backend
$env:PYTHONPATH = (Get-Location).Path
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\ruff.exe format --check app tests
.\.venv\Scripts\ruff.exe check app tests
.\.venv\Scripts\mypy.exe --strict --no-incremental app
```

Frontend:

```powershell
Set-Location frontend
npm.cmd ci
npm.cmd run typecheck
npm.cmd run lint
npm.cmd run build
npm.cmd audit --audit-level=high
```

PostgreSQL and Redis integration tests require explicit `TEST_POSTGRES_URL` and `TEST_REDIS_URL`. A skipped infrastructure test is not a verified infrastructure result.

## Evidence and governance

Evidence submissions are review-only. Files are hashed and retained locally, extracted claims preserve their source locations and original values, and conflicts require a human decision. Source rank never auto-verifies a claim.

Approval packs are independently scoped to rules, fees, risk limits, real datasets, strategy promotion, or campaign creation. Pack generation grants no approval and blanket approval is forbidden.

See [Evidence Workspace](docs/EVIDENCE_WORKSPACE.md), [Evidence Decision Workflows](docs/EVIDENCE_DECISION_WORKFLOWS.md), and [Known Limitations](docs/KNOWN_LIMITATIONS.md).

## Security

Do not use public issues for credentials, account statements, private portfolio data, or personal financial records. See [SECURITY.md](SECURITY.md) for reporting guidance.

The local API-key and role controls are intended for a trusted local operator environment, not an internet-facing multi-user service.

## Contribution status

External code contributions are not currently accepted while project licensing and governance are being decided. Non-sensitive bug reports and documentation observations are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md).

## License status

**No public reuse license has been granted.** No `LICENSE` file currently exists. Copyright law therefore reserves reuse, modification, and redistribution rights unless the copyright holder grants permission separately.
