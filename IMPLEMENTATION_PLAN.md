# Milestone 1 Implementation Plan

1. Foundation: typed settings, FastAPI lifecycle, SQLAlchemy models, migration, logging, health diagnostics, Docker, Windows setup.
2. Data: normalized schemas, provider contract, mock/CSV/bdshare/bdfinance adapters, validation, dual-source comparison, persistence.
3. Portfolio: immutable transactions, CSV/manual imports, derived holdings, valuation, P&L and DSEX benchmark endpoints.
4. Research: deterministic event-based backtester with fees, slippage, delayed fills, four initial strategies and JSON/HTML output.
5. Execution and risk: idempotent proposals, deterministic versioned risk decisions, emergency stop, approval revalidation, realistic paper fills.
6. Signals and notifications: versioned signals, paper-only proposals, Telegram and console alerts.
7. Dashboard: dark Next.js interface for overview, portfolio, signals, orders, risk, data health and audit events.
8. Verification: unit/integration/failure tests, backend lint/type/test, frontend lint/build, offline acceptance flow.
9. Delivery: complete operator/security/risk documentation and narrow Git commits in `E:\DSE AutoTrader`.

Live execution is structurally unavailable in this milestone. The broker factory refuses every adapter except the paper broker.

