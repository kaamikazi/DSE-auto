# DSE AutoTrader

A Windows-first DSE research, portfolio monitoring, backtesting and supervised **paper-trading** platform. Milestone 1 cannot submit real-money orders. The only executable broker is the local paper broker; `OfficialBrokerAdapter` always refuses calls.

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

## Windows quick start

```powershell
Set-Location 'E:\DSE AutoTrader'
Copy-Item .env.example .env
.\scripts\setup.ps1
.\scripts\start.ps1
```

Open `http://localhost:3000` and API docs at `http://localhost:8000/api/docs`. Change `API_SECRET_KEY`; mutating endpoints require it in `X-API-Key`.

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

See [ARCHITECTURE.md](docs/ARCHITECTURE.md), [WINDOWS_SETUP.md](docs/WINDOWS_SETUP.md), [PAPER_TRADING_GUIDE.md](docs/PAPER_TRADING_GUIDE.md), and [KNOWN_LIMITATIONS.md](docs/KNOWN_LIMITATIONS.md).

