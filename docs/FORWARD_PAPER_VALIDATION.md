# Minimal V1 forward paper validation

This local runner is the only current forward-validation surface. It is restricted to
`absolute_momentum_filter@0.1.0`, the frozen four-dataset identity, and the frozen 25-symbol
universe. It creates simulated paper effects only. Qualification remains `0/60`.

## Safety and evidence boundary

Startup refuses unless `TRADING_MODE=paper`, `LIVE_TRADING_ENABLED=false`, and
`BROKER_ADAPTER=disabled`. It also verifies the database role and health, canonical audit chain,
local clock, exact strategy registration/code/parameter hashes, timing and cost contracts, and all
four active dataset IDs and file hashes. The dedicated account defaults to BDT 1,000,000.

The database `paper_session_runs` rows are the authoritative append-only operational ledger. A
local JSONL projection is maintained under `data/process-state/minimal_v1_forward/<session-id>/`.
It records observations, decisions, targets, executions, costs, cash, holdings, reconciliation,
health, and explicitly separated replay/forward metrics. Replay records are never forward evidence.

Runtime display states are derived as `STOPPED`, `HEALTHY`, `DEGRADED`, or `HALTED`; no new
persisted lifecycle vocabulary was added. Order keys bind the strategy registration, rebalance
date, data snapshot, execution session, symbol, and side. The database uniqueness constraint and
durable execution plan prevent duplicate effects after restart.

## Operator commands

Run from `backend`:

```powershell
python -m app.minimal_v1_cli forward-status
python -m app.minimal_v1_cli forward-start --mode forward
python -m app.minimal_v1_cli forward-stop
python -m app.minimal_v1_cli forward-emergency "explicit operator reason"
python -m app.minimal_v1_cli forward-start --mode forward --resume-emergency
python -m app.minimal_v1_cli forward-portfolio
python -m app.minimal_v1_cli forward-decision
python -m app.minimal_v1_cli forward-reconcile
```

`forward-start` is a foreground service. A second instance fails on the local OS lock and reports
the existing owner. Emergency halt prevents new executions, preserves holdings/evidence, and
requires the explicit resume flag. Stop is cooperative and does not liquidate.

## Real-data blocker

The current `mock` provider is forbidden for forward evidence. Genuine collection remains blocked
until all 25 symbols have a trustworthy adjusted EOD source with validated lineage and
exchange-verified or operator-attested timestamps, and the DSE market/holiday calendar is
operator-verified. A certified forward-ingestion contract must then be added and independently
verified; the current build deliberately has none. Historical research files are accepted only in
isolated replay mode and never masquerade as current data.

Replay requires a copied, isolated database and the `test` database role:

```powershell
$copy = Join-Path $env:TEMP "dse-forward-replay.db"
Copy-Item .\data\dse_autotrader.db $copy
$env:APP_ENV = "test"
$env:DATABASE_ROLE = "test"
$env:DATABASE_URL = "sqlite:///$($copy.Replace('\','/'))"
python -m app.minimal_v1_cli forward-start --mode replay `
  --start-date 2025-01-01 --end-date 2025-07-10
```

Delete the copy after inspection. Never point a test/simulation role at the operational database.

## Windows Task Scheduler

Create a task running only after the operator has resolved the displayed provider/calendar blockers:

- Program: `E:\DSE AutoTrader\backend\.venv\Scripts\python.exe`
- Arguments: `-m app.minimal_v1_cli forward-start --mode forward`
- Start in: `E:\DSE AutoTrader\backend`
- Trigger: at logon or boot after local storage is available
- Failure policy: restart after one minute, with a bounded retry count
- Concurrency: **Do not start a new instance** when the task is already running

The OS lock remains the final duplicate barrier. Console alerts and the bounded 1 MB rotating file
`logs/minimal_v1_forward_alerts.log` are local only; no Telegram or external messaging is used.
