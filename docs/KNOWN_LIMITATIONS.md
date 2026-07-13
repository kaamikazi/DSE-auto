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
- **Historical Audit Branch**: The legacy two-writer branch remains preserved in its hash-addressed archive; it is not part of the valid canonical generation.
- **Legacy Audit Evidence**: The historical branch is archived and preserved; only the new canonical generation is operationally valid.
- **Public DSE Providers**: bdshare remains blocked by TLS/DNS failures, and no bdfinance distribution was available from the configured package index.
- **Attestation Scope**: Operator-attested CSV validates workflow reliability, not live-market latency or provider accuracy.
- **Rule and Fee Verification**: Milestone 6 defaults remain assumptions until checked against current official and broker documents.
- **Attestation Truth**: Hashing proves file immutability, not that a human-attested source is correct.
- **Campaign Valuation**: Current summaries emphasize account cash snapshots; robust mark-to-market needs sustained trustworthy DSE history.
- **Accelerated Campaign**: Twenty simulated sessions exercise workflows and recovery branches, not strategy profitability.
- **Local Persistence**: SQLite and in-process APScheduler are not multi-host, high-availability infrastructure.
- **Alert Independence**: Telegram plus console fallback is not independent notification redundancy.
- **Local Metrics**: Metrics endpoints assume localhost isolation and do not replace authenticated monitoring.
- **Editable Environment**: The existing `.venv` has stale editable-install metadata for another workspace. Verification pins `PYTHONPATH` to this repository; recreate `.venv` before routine operation.

## Milestone 7 limitations

- **Distributed runtime unverified**: Docker Compose syntax passes, but the Docker Desktop Linux daemon was unavailable. PostgreSQL migrations, Redis worker delivery, service restarts, PostgreSQL backup/restore, and the Docker 30-day harness were not executed.
- **Emulation is not qualification**: The 30-day SQLite/in-memory run validated workflow logic only. It produced 29 qualifying days and leaves 31; none are claimed as real-market qualification days.
- **No trustworthy live DSE feed**: bdshare still fails verified TLS/DNS and lacks exchange timestamps. No published bdfinance distribution exists on the configured index. The fake certified adapter is test-only.
- **No PostgreSQL replica/failover**: Health metadata is implemented, but no replica, failover manager, or measured failover has been deployed.
- **Outbox boundary**: Delivery is at-least-once. Idempotent consumers can make their database effect once; the overall distributed system is not exactly once.
- **Python lock/advisories**: Dependencies have bounded major-version ranges, not a fully hashed production lock, and no Python vulnerability scanner is integrated.
- **Local authentication**: Role separation, expiring sessions, throttling, and restricted audit access are local controls; there is no enterprise identity provider or hardware-backed key storage.
- **Notification independence**: Telegram/console fallback does not meet independent out-of-band alerting requirements.
- **Data-quality thresholds**: Default thresholds exercise fail-closed logic but require calibration and independent approval against a licensed real feed and actual DSE sessions.
- **Backup encryption**: ACL restriction is automated; encrypted secret/database storage depends on an external approved encryption/key-management process.
