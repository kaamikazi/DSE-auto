# Known Limitations

- **No Real-Money Broker Execution**: The system is strictly paper-trading only. The real-money broker adapter is completely inactive.
- **Scraped Data Lack Timestamps**: The `bdshare` provider collects quotes via public web scraping, which lacks official exchange execution timestamps. Consequently, the system blocks live approvals when using `bdshare` in production to prevent stale-data slippage.
- **Provider Package Availability**: The `bdfinance` provider is unavailable on the host workspace due to missing library installation. It dynamically reports as unavailable.
- **Downgrade Safety Protections**: If the primary data provider fails and the circuit breaker falls back to a provider lacking verified exchange timestamps (like `bdshare`), the system dynamically flags the data as unsafe, blocking any new order proposals or approvals.
- **Corporate-Action Adjustments**: Dividends, splits, rights, and bonus share adjustments depend on manual transaction input logs to reconcile cost basis.
- **Operator-Grade Security**: The API key mechanism (`X-API-Key`) is designed for local operators and single-process development, not multi-user internet-facing environments.
- **No Leverage or Short Selling**: Transactions and backtests simulate cash-only (long-only) operations without leverage.
- **Single Scheduler Process**: Embedded APScheduler is not a distributed queue and must be enabled in only one API process.
- **Process-local Breakers**: Provider circuit-breaker state resets on process restart; job outcomes remain persisted.
- **Timestamp Gate**: bdshare or bdfinance records without a trustworthy market timestamp remain research-only.
- **No Profitability Claim**: Strategy reports are validation evidence, not forecasts or investment advice.
- **Real-provider Blocker**: The latest bdshare smoke test encountered TLS/DNS failures; bdfinance was not installed in the active environment. Real DSE validation is not yet continuous or trustworthy.
- **Calendar Governance**: Configured hours and bands are explicit simulation assumptions that require independent confirmation against current DSE publications.
- **Evidence Charts**: The evidence service emits HTML/JSON/CSV; richer chart exports and a full shadow-portfolio UI remain follow-up validation work.
- **Audit Remediation Required**: The current restored database has a historical two-writer audit branch. Do not run a continuous paper session until an operator-approved archive/new-chain procedure and serialized audit writer are implemented.
