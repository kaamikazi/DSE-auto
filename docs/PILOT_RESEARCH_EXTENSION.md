# BATBC/SQURPHARMA Research Extension

The BATBC/SQURPHARMA extension is an independently versioned, linked research
dataset. It does not rewrite the active GP/ACI/BRACBANK parent dataset.
Qualification remains **0/60**.

## Registry

- Classification: **RESEARCH DATASET ACTIVE**
- Dataset/version: `batbc-squrpharma-t2-extension-5357a454f66e1ea7`
- Registry ID: `c6da44f7-b842-4d0a-a8e0-31bad7f96bea`
- Dataset SHA-256:
  `4470633c8d62c627357e6c0a6472466142e7a6539f7100d9de677827dda8c882`
- Parent registry ID: `ba5f2d99-6c66-4e37-ae31-d48c8ee47b15`
- Parent version: `gp-aci-bracbank-research-f24a48cb729e8a65`
- Parent SHA-256:
  `ddfeee6bbf5324f9f64fd604e9d4bfc7bd2a45ce2896e7b968037af58de04791`
- Transformation: `batbc-squrpharma-t2-extension-v1`
- Activation Git HEAD: `0d068d6c908ac58ed8fe2d6dbdf5b196b4491b84`
- Canonical audit chain: `c7aa6ed0-1288-417f-acbb-6ad4bfdd967c`

This classification does not mean exchange verified, officially lifecycle
verified, paper candidate, strategy approved, production ready, or real-money
ready. All those properties are explicitly false in the registry record.

## Activated and excluded rows

Only `tier_2_single_source_high_quality` rows were activated:

| Symbol | T2 active | T3 excluded | Invalid excluded | Duplicate conflict excluded | Lifecycle excluded |
|---|---:|---:|---:|---:|---:|
| BATBC | 6,255 | 6,262 | 15 | 14 | 0 |
| SQURPHARMA | 6,317 | 6,328 | 15 | 14 | 1 |
| **Total** | **12,572** | **12,590** | **30** | **28** | **1** |

Every active symbol/date/adjustment-grain key is unique. OHLC invariants,
known adjustment grain, high-confidence mapping, complete lineage, and the
approved observed window were verified for all 12,572 rows. IDLC, LANKABAFIN,
and POWERGRID remain inactive and explicitly rejected/not granted.

The accepted 2012-10-01 through 2026-01-22 boundary is an observed research
window, not an official listing-date claim. Lifecycle evidence remains pending.

## Source policy

The Mendeley known-grain coverage-metadata row is primary. DSEStocks and
AmarStock rows remain validation references where overlapping. The extension
preserves raw file hashes, source row IDs, mapping status, and raw OHLCV lineage.
It does not claim source independence, average values, reconstruct corporate
actions, or infer missing dates. Timestamp trust remains `unknown` and source
trust is `third_party_research`.

## Decisions and audit

Eleven governance records and eleven separate canonical audit events preserve:
BATBC/SQURPHARMA T2 approvals; their T3 rejections; lifecycle-pending treatment;
invalid and duplicate-conflict exclusions; individual IDLC, LANKABAFIN, and
POWERGRID rejections; dataset activation; and strategy-execution prohibition.
The canonical chain is valid with 232 events after activation.

The evidence pack is under
`reports/pilot_research_extension/batbc-squrpharma-t2-extension-5357a454f66e1ea7/`.
Its manifest hash is
`6b8bb08a02e00e33d504109d23101653ec0efce6ce0c2011ad3ea5f1c75f5b8a`.

## Five-symbol plan — not executed

The prepared universe is GP, ACI, BRACBANK, BATBC, and SQURPHARMA. The plan
contains planned per-symbol and buy-and-hold result sections, equal-weight and sector-balanced
portfolios, leave-BRACBANK-out, leave-best-symbol-out using training data only,
and complete leave-one-symbol-out tests. Validation is chronological walk-forward
with an untouched holdout, parameter sensitivity, and cost/slippage sensitivity.

Source-tier, lifecycle, corporate-action, and benchmark limitations remain
explicit. The plan status is `prepared_not_authorized_not_executed`.
`ma_crossover@1.0.0` remains in research state and was not run or promoted. No
campaign, session, proposal, order, transaction, or fill was created.
