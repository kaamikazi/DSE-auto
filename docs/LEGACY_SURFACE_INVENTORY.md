# Legacy surface inventory

Minimal V1 (`python -m app.minimal_v1_cli`) is the canonical research operator interface. This
inventory changes documentation status only: no file, import, automated workflow, stored record,
or historical evidence was altered or removed.

Classification meanings:

- `retained_dependency`: a current test, script, or compatibility path still imports or invokes it.
- `historical_read_only`: retained to explain or reproduce preserved historical evidence.
- `deprecated_operator_surface`: not permitted for new operator work, but not yet approved for archival.
- `archive_candidate`: no active import or required automated workflow was found; archival still requires separate authorization.
- `still_required`: current safety, verification, recovery, or canonical documentation surface.

## Inventory

| Surface | Kind | Classification | Dependency/evidence finding | Direct-use policy |
|---|---|---|---|---|
| `scripts/operator.py` | superseded operator CLI | `retained_dependency` | Invoked by `start_production_like.ps1`, `start_low_memory_substage.ps1`, `verify_real_dse_data.ps1`, and `paper-operator.ps1`. | **Deprecated for new operator work.** Retain until callers are separately migrated. |
| `scripts/real_market_operator.py` | superseded operator CLI | `retained_dependency` | Invoked by `m10-operator.ps1` and `m10-eod.ps1`. | **Deprecated for new operator work.** Historical paper qualification only. |
| `scripts/paper-operator.ps1` | PowerShell menu wrapper | `deprecated_operator_surface` | Calls `scripts/operator.py`; no required CI workflow invokes it. | **Deprecated.** Do not use for new research operations. |
| `scripts/m10-operator.ps1` | Milestone 10 wrapper | `deprecated_operator_surface` | Calls `real_market_operator.py`; no required CI workflow invokes it. | **Deprecated.** Preserve pending a separate caller-removal decision. |
| `scripts/run_historical_strategy_research.py` | historical runner | `retained_dependency` | Imported by `test_historical_strategy_research.py`; its preserved output anchors later evidence. | **Deprecated for direct execution.** Retain test and evidence compatibility. |
| `scripts/run_five_symbol_robustness.py` | historical runner | `retained_dependency` | Imported by `run_risk_control_attribution.py` and `archive_rejected_strategy.py`. | **Deprecated for direct execution.** Retain until historical imports are flattened separately. |
| `scripts/run_risk_control_attribution.py` | historical runner/report generator | `retained_dependency` | Read by `test_risk_control_attribution.py`; preserved result is also read by hash rather than regenerated. | **Deprecated for direct execution.** Retain while the source-contract test depends on it. |
| `scripts/run_strategies.py` | early backtest runner/report generator | `archive_candidate` | No inbound import or required workflow found. | Do not use; Minimal V1 owns current reproduction. |
| `scripts/m10-eod.ps1` | milestone PowerShell wrapper | `archive_candidate` | No inbound invocation or required workflow found; it only calls the deprecated Milestone 10 CLI. | Do not use for new operations. |
| `scripts/run_m10_five_day_dry_run.py` | milestone dry-run runner | `archive_candidate` | No inbound import or required workflow found. | Historical exercise only; do not rerun. |
| Embedded JSON/CSV/Markdown/HTML and manifest helpers in the three historical research runners | duplicate report generators | `historical_read_only` | Preserved evidence and hashes depend on their historical output shape; Minimal V1 does not call them. | Retain with the historical runner until archival is authorized. |
| Backup, restore, audit verification, secret scan, and infrastructure-doctor scripts | operational safeguards | `still_required` | Referenced by current verification/recovery guidance or CI-adjacent operations. | Continue using only for their documented safety purpose. |
| Milestone campaign, qualification, approval, incident, and infrastructure result documents | milestone-specific documentation | `historical_read_only` | Explain preserved decisions and evidence; links may remain from historical documents. | Not current getting-started guidance; do not rewrite historical claims. |
| `README.md`, `MINIMAL_V1.md`, `DAILY_OPERATOR_RUNBOOK.md`, `KNOWN_LIMITATIONS.md`, `SECURITY.md`, and `VERIFICATION.md` | current documentation | `still_required` | Current product boundary, canonical path, limitations, security, and verification record. | Maintain as the current documentation set. |

## Dependency verification

Repository-wide exact-name and Python-import scans covered `backend`, `scripts`, `frontend`,
`.github`, tests, README, and documentation. The scan reclassified
`run_risk_control_attribution.py` as `retained_dependency` because a focused test reads its
source. GitHub Actions invokes none of the three remaining `archive_candidate` surfaces; no
backend module or test imports them, and no other script invokes them.
Documentation references do not constitute an active runtime dependency and must be preserved
until an authorized archival pass updates links without changing historical evidence.

This inventory authorizes no deletion, move, migration, state change, or audit event.
