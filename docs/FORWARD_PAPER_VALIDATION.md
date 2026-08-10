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
$attestation = 'I manually obtained this official DSE public EOD/archive file, the stated market session had completed, and these observations were visible when I acquired it.'
python -m app.minimal_v1_cli forward-ingest `
  --file C:\path\to\manually-obtained-dse-eod.html `
  --market-date 2026-08-11 `
  --source official_dse_public_eod_archive `
  --session-completed-at 2026-08-11T14:10:00+06:00 `
  --attestation $attestation
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

## Operator-attested manual EOD boundary

`forward-ingest` reads one local, single-market-date CSV or saved HTML archive file. It performs no
download and accepts only the exact source identity `official_dse_public_eod_archive`. The operator
must obtain the file manually through ordinary access and provide the exact attestation shown
above. AmarStock automation, HTTP clients, browser automation, TLS bypasses, external messages,
and broker access are absent from this path.

The command captures its own UTC receipt timestamp and records the separately attested session
completion time. Availability is the later of those values. It refuses a session completed before
the current committed implementation boundary, so pre-existing August/replay files cannot be
relabelled as forward evidence. The command also refuses tracked working-tree changes; the first
accepted observation must therefore come from a session completed after the final implementation
commit.

Raw bytes are retained under
`data/process-state/minimal_v1_forward/<session-id>/manual_eod/<date>/<raw-sha256>/`. The raw file,
canonical normalized JSON, and evidence JSON are write-once identities. The database and JSONL
ledgers bind original filename, hashes, parser version, source, attestation, receipt/availability,
full source symbol set, row count, and missing/unavailable symbols. Re-ingesting identical bytes is
idempotent. A corrected file receives a new hash/version and links to, but never overwrites, the
prior event or availability time.

DSE public observations are always `raw_unadjusted`. Missing, suspended, or nonpositive source
rows are surfaced and never synthesized. The runner can consume the accepted evidence, but it
records no decision or trading effect while the adjusted analytical view, corporate-action
evidence, or period-ending calendar evidence is unresolved. The automated-provider readiness
assessment remains blocked and uncertified.

Historical research files are accepted only in isolated replay mode and never masquerade as
current data.

This boundary adds one CLI command and no table, migration, persisted lifecycle state,
audit-event type, provider abstraction, or report format. It reuses `paper_session_runs`, the
existing JSONL projection, and the existing `data_import.activated` audit type.

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
