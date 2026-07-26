# Known Limitations

## Milestone 11 governed research data

- No external DSE, DSEX, broker, or licensed-vendor file was supplied or downloaded; verification uses deterministic fixtures only.
- XLSX and Parquet require their controlled pandas engines to be installed. Missing engines fail closed.
- The portfolio analytics foundation does not infer missing realized returns, dividends, volatility, liquidity, or corporate actions.
- Dataset research activation is not market truth, strategy promotion, campaign qualification, or trading authorization.
- Official rules, fees, account terms, timestamps, corporate actions, and broker/API availability still require human-reviewed evidence.

## Authoritative evidence remains unavailable

No authoritative DSE/broker/account evidence or approved real-market research dataset has been supplied. Registry and workflows are implemented, but all rule, fee, and risk matrices remain unapproved. Deterministic fixtures validate mechanics only and are neither real-market evidence nor profitability evidence. Operator reviewer's review is non-independent whenever he is also operator.

## Corrected-B2 completion limitations — 2026-07-15

- The passed three/ten-day campaign is accelerated single-host infrastructure validation, not elapsed DSE sessions, representative market evidence, profitability evidence, or live-trading readiness.
- Two workers, PostgreSQL, Redis, and a scheduler were verified together on this host; there is still no multi-host failover, PostgreSQL replica, Redis HA, or independent control plane.
- Two initial competition harness attempts were aborted due verification-harness defects. Preserved evidence distinguishes them from the later valid pass.
- Campaign checkpoint dumps were non-restorable because Windows PowerShell 5.1 converted binary stdout to UTF-16. Backup/restore scripts are corrected and a later isolated restore passed, but the original dump files remain invalid evidence.
- The fresh PostgreSQL restore was local to the same Docker host, not a clean-machine or cross-host disaster-recovery rehearsal.
- Dependency and secret scans are point-in-time. The test-only pytest pin required an advisory-driven update to 9.0.3 during final verification.
- Public DSE provider timestamp trust, licensed feed certification, 60 independently reviewed real-market paper days, broker approval, legal/compliance, encrypted backups/KMS, and independent alerts remain unresolved.

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

## Milestone 8 limitations

- **Machine preflight blocked**: Docker Desktop is installed, but its service/Linux engine was stopped during verification. PostgreSQL, Redis, real workers/scheduler, restarts, and the distributed ten-day campaign remain unverified.
- **Memory headroom**: Available RAM was below the required 4 GB preflight threshold. The start script fails closed until the operator frees memory.
- **Offline incidents are not outages**: Controlled incident reports prove audit/fail-closed workflow behavior, not real process recovery. Real evidence requires the Docker harness.
- **No licensed feed certified**: The certification framework exists; the fake adapter fails its test-only license. Zero real-market days exist.
- **Single-host topology**: The production-like profile has durable stores and independent processes but no PostgreSQL replica, Redis HA, multi-host orchestration, or automatic host failover.
- **Recovery source database**: The clean-machine bundle currently backs up SQLite. PostgreSQL-native clean-machine backup/restore evidence remains blocked with Docker.
- **Secrets and encryption**: Bundles exclude secrets, but independent key management, encrypted database backups, credential rotation, and enterprise identity remain external requirements.
- **Audit freshness**: Vulnerability and license inventories are point-in-time evidence and must be regenerated before each release.
- **Research extra**: `quantstats` is intentionally excluded from operational locks and must not be installed into the paper runtime without separate pinning/review.
- **Existing virtual environment**: The checked-out `.venv` contains stale editable metadata referring to the independent Antigravity workspace. Verification explicitly pins `PYTHONPATH` to this repository; operators should replace that environment from the hash-locked requirements.
- **Optional Playwright metadata**: `@playwright/test` appears only as an optional peer entry in `package-lock.json`; it is not an application dependency or browser-automation implementation.
- **Provider timestamp trust**: Both current public-provider diagnostic reports classify exchange timestamp support as unsupported. Neither provider can authorize paper-order approval under the trusted-timestamp gate.

## Milestone 9 staged limitations

- Stage A PostgreSQL and Redis integration tests are real; Stage B worker behavior and Stage C campaign behavior remain unverified.
- Starting the four Stage B application containers reduced available memory to 2.38 GiB, below the retained 3 GiB runtime margin.
- Commit headroom is ample because of the 23 GiB pagefile, but paging capacity is not treated as a substitute for physical memory.
- SQLite-to-PostgreSQL operational-copy comparison, real restart/outage exercises, distributed campaign, and portfolio onboarding expansion remain blocked/not run.

## Low-memory continuation status (2026-07-15)

- B2 failed due host paging/memory trend; two-worker competition, worker crash/stale lease, dead-letter replay, and all-process simultaneous validation remain blocked.
- PostgreSQL-mid-task restart remains not run because no safe long-running retry fixture exists.
- The 3-day and 10-day accelerated distributed campaigns remain blocked/not run; no real-market evidence exists.
- API, scheduler, and queued-task Redis restart passed only as real serialized B3 exercises.
- SQLite-to-PostgreSQL copy passed in an isolated database; primary-database cutover and PostgreSQL-native backup/restore remain separate blockers.
- Portfolio isolation is test-verified, but no actual real-account statement was imported and no broker credentials/access were used.
- This 16 GiB host remains sensitive to unrelated Oracle/MySQL/SQL Server and user-process pressure.

## Paging-measurement correction (2026-07-15)

- The previous B2 failure reason is invalid because `PageReadsPersec + PageWritesPersec` was labelled “hard paging” and averaged startup activity with steady state.
- The preserved run is not retroactively accepted. B2 remains blocked until a new 120-second-warm-up plus 600-second-steady-state observation uses the corrected counters.
- Windows hard-fault disk reads can be executable, DLL, mapped-file, or pagefile backed. The available counters do not directly attribute every input page to `pagefile.sys`.
- The corrected decision therefore requires sustained hard-fault reads plus memory, pagefile, disk, scheduler, heartbeat, process, or database impact.
## Milestone 10 limitations

- Real-market qualification is `0/60`. The five-day workflow dry-run is synthetic/test evidence and is permanently excluded.
- No licensed provider currently passes. bdshare fails verified TLS/DNS access and cannot supply a trustworthy exchange timestamp; the published bdfinance runtime is unavailable. Operator-attested reviewed files are supported but none were supplied for an actual session.
- No real DSE campaign has been created or started. EOD, review, and weekly workflows are implementation/test evidence only until genuine daily files and a human reviewer are available.
- Reference's portfolio workflow is test-verified, but no actual real-account statement was imported. It remains read-only and isolated from paper holdings.
- PostgreSQL and Redis integration tests passed, but this milestone did not rerun Milestone 9's resource-intensive distributed B2/campaign exercises.
- Market rules, fee assumptions, risk thresholds, execution/slippage assumptions, licensed data rights, independent monitoring, encrypted backup key management, legal/compliance approval, and official broker documentation still require independent approval before any future real-money design.
- Live execution remains absent and disabled. No broker login, password, PIN, OTP, CAPTCHA, Selenium, browser automation, unofficial endpoint, AI approval, or live order path was added.

## Milestone 11 limitations

- The workspace implements evidence collection and review mechanics; no genuine new DSE, broker, account, or licensed market-data evidence was supplied during implementation.
- Deterministic extraction supports CSV/XLSX and simple text-bearing documents. Scanned PDFs and images require careful manual transcription and independent review.
- Source hierarchy is an aid, not a truth oracle. Authenticity, applicability, effective date, supersession, and conflicts still require a human decision.
- Portfolio-statement parsing is a draft workflow and does not cover every broker-specific layout. It deliberately creates no ledger transactions or holdings.
- Dataset quality checks are screening controls, not proof that a source is licensed, complete, exchange-timestamped, or suitable for real-market qualification.
- Approval packs are point-in-time summaries. They cannot approve, activate, promote, create a campaign, or start trading.
- The repository virtual environment may retain editable-package metadata from the independent Antigravity copy. Verification pins `PYTHONPATH` to this repository; rebuilding the environment from locked requirements is recommended.
- Real-market qualification remains `0/60`; profitability and live-trading readiness are not claimed.
