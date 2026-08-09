# Minimal V1 research facade

> **Canonical operator interface.** Use this CLI for current research status, discovery,
> historical-run review, and archived-result reproduction. Legacy operator CLIs, wrappers, and
> milestone runners are deprecated for direct use; their retention status is recorded in
> [LEGACY_SURFACE_INVENTORY.md](LEGACY_SURFACE_INVENTORY.md).

## Purpose and boundary

Minimal V1 is a compact facade over existing DSE AutoTrader records, files, and the trusted
backtest engine. Its research commands remain read-compatible. Its separately documented forward
paper surface may create only the dedicated, local simulated records authorized for
`absolute_momentum_filter@0.1.0`; it is not a schema migration, replacement registry, approval
workflow, or new strategy.

The permanent boundary remains:

- `TRADING_MODE=paper`
- `LIVE_TRADING_ENABLED=false`
- `BROKER_ADAPTER=disabled`

`app.core.config.assert_paper_only_safety` is the single imported assertion used before any
reproduction. Existing `Settings` validation remains the broader application startup boundary.

## Read models and legacy mappings

| Minimal model | Canonical source | Derivation |
|---|---|---|
| `SafetyStatus` | `Settings`, `DATABASE_ROLE`, `verify_audit_chain` | Reports the loaded configuration, database role, and current audit verification result. |
| `DatasetSummary` | `research_datasets`, normalized JSONL file | Maps registry ID/name/hash/symbols/status and quality-report coverage/row counts; lineage is complete only when the immutable-lineage marker exists and the normalized file hash matches. |
| `StrategySummary` | `strategy_registrations` | Maps registration identity and code hash; parameter hash and permissions come from existing evidence, defaulting permissions to false when absent. |
| `ResearchRunSummary` | Archived `ma_crossover` contract, pinned risk-control result, pinned five-symbol result | Verifies both evidence hashes, then exposes timing, costs, benchmark, principal metrics, verdict, and artifact locations. |

No Minimal V1 read model inherits SQLAlchemy `Base`, owns a `__tablename__`, or persists state.

## Facade and CLI

`MinimalV1Facade` supports safety status, active research datasets, registered strategies,
historical run lookup, and reproduction through the existing five-symbol engine. The only new
and canonical operator surface is:

```powershell
cd backend
python -m app.minimal_v1_cli status
python -m app.minimal_v1_cli datasets
python -m app.minimal_v1_cli strategies
python -m app.minimal_v1_cli runs [run-id]
python -m app.minimal_v1_cli reproduce [run-id] [--output-dir PATH]
python -m app.minimal_v1_cli forward-status
python -m app.minimal_v1_cli forward-start --mode forward
python -m app.minimal_v1_cli forward-stop
python -m app.minimal_v1_cli forward-emergency "explicit operator reason"
python -m app.minimal_v1_cli forward-portfolio
python -m app.minimal_v1_cli forward-decision
python -m app.minimal_v1_cli forward-reconcile
```

Campaign, qualification, reviewer, provider-certification, approval, and broker commands are
not reachable through this CLI. See [FORWARD_PAPER_VALIDATION.md](FORWARD_PAPER_VALIDATION.md)
for the dedicated session, isolated replay, crash recovery, emergency stop, data blockers, and
Windows Task Scheduler contract.

## Reproduction contract

The compatibility target is the immutable five-symbol `ma_crossover@1.0.0` result linked from
the archived strategy registration. Reproduction verifies:

1. canonical audit validity and the centralized paper-only assertion;
2. registration, code, parameter, parent dataset, extension dataset, and file hashes;
3. the existing adjusted execution bars and next-source-present-open engine;
4. fees of 0.40% and slippage of 0.25%;
5. per-symbol returns, combined return, drawdown, event/closed-trade counts, buy-and-hold, and
   leave-BRACBANK-out return at `1e-8` absolute tolerance, with counts exact; and
6. the exact verdict `reject_strategy / archived_rejected_benchmark`.

A successful run writes only `research_result.json`, `trade_ledger.csv` when trades exist, and
`interpretation.md`. JSON contains source/dataset/Git/database/audit provenance, the comparison
values and tolerances, hashes for the CSV and Markdown artifacts, and a canonical payload hash.
It creates no HTML, manifest, approval pack, safety report, or audit event.

The 2026-07-30 operational compatibility run reproduced all eleven measured fields with zero
difference. Combined return was 147.79556445495152%, maximum drawdown -21.34889030517515%,
225 trade events (111 closed), buy-and-hold 191.4870783761503%, and leave-BRACBANK-out
44.10586751181194%. The five per-symbol return differences were also zero.

Before/after database counts were identical: three campaigns, five paper sessions, 153 signals,
five orders, two transactions, 658 total audit events, two research datasets, and five strategy
registrations. The canonical chain remained valid with 260 canonical events.

## Legacy surfaces identified, not archived

- Redundant runners: `run_historical_strategy_research.py`,
  `run_five_symbol_robustness.py`, and `run_risk_control_attribution.py`.
- Duplicate report generation: the JSON/CSV/Markdown/HTML and manifest helpers embedded in those
  runners. They remain the historical source but are not used for new Minimal V1 packaging.
- Superseded operator surfaces: `scripts/operator.py`, `real_market_operator.py`,
  `paper-operator.ps1`, `m10-operator.ps1`, and milestone-specific PowerShell launchers.
- Fragmented documents: milestone-specific campaign, qualification, infrastructure, approval,
  and evidence-pack guides. No document is moved until references and historical value are
  reviewed separately.

All legacy tables, files, reports, hashes, and audit events remain unchanged and readable.

## Complexity budget

Measurements use physical Python lines, including comments and blanks.

| Measure | Before | After | Delta | Limit/result |
|---|---:|---:|---:|---|
| Production Python files under `backend/app` | 113 | 116 | +3 | Read models, facade, CLI only |
| Production LOC under `backend/app` | 30,606 | 31,249 | +643 | Recorded; no explicit LOC ceiling |
| Minimal V1 executable entrypoints | 0 | 1 | +1 | Pass: at most 1 |
| Minimal V1 CLI commands | 0 | 5 | +5 | Pass: at most 5 |
| Database tables | 52 | 52 | 0 | Pass |
| Alembic revisions | 12 | 12 | 0 | Pass |
| Lifecycle/state declarations | unchanged | unchanged | 0 | Pass |
| Audit-event types | unchanged | unchanged | 0 | Pass |
| Report formats | JSON/CSV/Markdown/HTML | unchanged | 0 | Pass; Minimal V1 emits only three existing formats |

The next simplification step may archive only items classified `archive_candidate` in the
legacy-surface inventory, and only after a separately authorized evidence-preserving plan.
