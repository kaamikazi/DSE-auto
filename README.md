# DSE AutoTrader

A Windows-first DSE research, portfolio monitoring, backtesting and supervised **paper-trading** platform. Milestone 7 cannot submit real-money orders. The only executable broker is the local paper broker; `OfficialBrokerAdapter` always refuses calls.

## Included

- FastAPI, Pydantic and SQLAlchemy backend with SQLite fallback and PostgreSQL configuration
- Normalized market-data contract with mock, CSV, bdshare and bdfinance adapters
- Data freshness/provider-disagreement validation
- Append-only portfolio transactions and derived holdings/P&L
- Deterministic buy-and-hold, 20/50 MA, momentum+DSEX and volume-breakout backtests
- Delayed fills, fees, slippage, liquidity limits and walk-forward partitions
- Versioned signals, deterministic pre-trade risk, idempotent proposals and approval revalidation
- Partial-fill paper broker, emergency stop and hash-chained audit events
- Telegram notifier with console fallback
- Dark Next.js dashboard with permanent safety banners
- Alembic, Docker Compose, PowerShell setup and offline test fixtures
- PostgreSQL-ready pooled/retry-safe persistence plus non-destructive SQLite migration tooling
- Redis-backed external scheduler/worker processes with leases, heartbeats, recovery and dead letters
- Durable versioned outbox events with replay and idempotent consumer-effect records
- Vendor-neutral data-adapter SDK, quality evidence, human daily reviews and a 60-day tracker
- Review-only evidence cases, deterministic claim extraction, conflict reporting, decision assistants, statement/dataset drafts, completeness tracking, and scoped approval packs

## Windows quick start

```powershell
Set-Location 'E:\DSE AutoTrader'
Copy-Item .env.example .env
.\scripts\setup.ps1
.\scripts\start.ps1
```

Open `http://localhost:3000` and API docs at `http://localhost:8000/api/docs`. Change both `API_SECRET_KEY` and `REVIEWER_API_SECRET_KEY`; operator mutations require the former, while protected evidence reads/reviews accept the appropriate role.

Manual start:

```powershell
Set-Location backend
.\.venv\Scripts\alembic.exe upgrade head
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload
Set-Location ..\frontend
npm.cmd run dev
```

## Verification

```powershell
Set-Location backend
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\ruff.exe check .
Set-Location ..\frontend
npm.cmd run typecheck
npm.cmd run build
```

## Safety invariant

`TRADING_MODE=paper` and `LIVE_TRADING_ENABLED=false` are validated at process startup. Any live setting causes configuration failure. Market orders are rejected. Stale/unsafe data, provider conflicts, non-healthy kill switch, excessive exposure, duplicate identifiers and reconciliation failures fail closed.

See [POSTGRESQL_OPERATIONS.md](docs/POSTGRESQL_OPERATIONS.md), [WORKER_ARCHITECTURE.md](docs/WORKER_ARCHITECTURE.md), [EVENT_BUS.md](docs/EVENT_BUS.md), [PAPER_TRADING_GUIDE.md](docs/PAPER_TRADING_GUIDE.md), and [KNOWN_LIMITATIONS.md](docs/KNOWN_LIMITATIONS.md).

## Milestone 11 evidence workspace

Milestone 11 adds a local evidence inbox and review workflow. Uploads are hashed and retained, deterministic extraction preserves source locations and original values, conflicts remain visible, and rule/fee assistants plus scoped approval packs prepare—not make—human decisions. Portfolio statements and market datasets remain drafts. No upload, extraction, review, or pack activates configuration or creates a campaign, session, proposal, order, transaction, or fill.

See [EVIDENCE_WORKSPACE.md](docs/EVIDENCE_WORKSPACE.md) and [EVIDENCE_DECISION_WORKFLOWS.md](docs/EVIDENCE_DECISION_WORKFLOWS.md).

## Milestone 2 operations

Persistent job records, overlap prevention, bounded retry/backoff, stale-worker recovery, provider failover, restart reconciliation and Telegram controls support reliable paper operations. Run APScheduler in exactly one process; set `SCHEDULER_ENABLED=false` on additional API workers. Telegram access is fail-closed through `TELEGRAM_ALLOWED_CHAT_IDS`; every one-time approval is revalidated immediately before paper execution.

## Milestone 3 paper validation

Named persistent paper sessions, an auditable Bangladesh calendar, conservative DSE execution rules, reversible imports, opt-in real-provider diagnostics and evidence packs support continuous paper validation. Start with `scripts\paper-operator.ps1`. Real broker execution remains unavailable.

## Milestone 6 sustained campaigns

Persistent multi-day campaigns, missed-session/EOD recovery, operator-attested quote/OHLCV/DSEX imports, immutable rule and fee versions, strategy governance, incidents, campaign analytics, and local operational metrics support several-week paper evidence collection.

Run the deterministic 20-session verification from `backend` with this repository pinned:

```powershell
$env:PYTHONPATH = (Get-Location).Path
.\.venv\Scripts\python.exe ..\scripts\operator.py simulate-campaign
```

See [CAMPAIGN_OPERATIONS.md](docs/CAMPAIGN_OPERATIONS.md), [DAILY_OPERATIONS.md](docs/DAILY_OPERATIONS.md), and [DATA_IMPORT_ATTESTATION.md](docs/DATA_IMPORT_ATTESTATION.md). Results are paper evidence, not proof of profitability.

## Milestone 7 production-like paper infrastructure

Production-like mode requires PostgreSQL, Redis, one external scheduler, and one or more workers. SQLite and in-process scheduling remain development/test modes. Daily evidence must pass data-quality gates and human review before it counts toward the 60-day qualification target.

The completed local 30-day exercise is explicitly an emulation: 29 days qualified after one injected rejected review, leaving 31. Docker Desktop was unavailable, so PostgreSQL/Redis integration and actual service-restart verification remain blocked. See [VERIFICATION.md](docs/VERIFICATION.md), [DAILY_EVIDENCE_REVIEW.md](docs/DAILY_EVIDENCE_REVIEW.md), and [PAPER_QUALIFICATION_TRACKER.md](docs/PAPER_QUALIFICATION_TRACKER.md).
