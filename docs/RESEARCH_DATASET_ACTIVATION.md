# Research-only target dataset activation

Status: **RESEARCH DATASET ACTIVE**  
Qualification: **0/60**  
Version: `gp-aci-bracbank-research-f24a48cb729e8a65`  
Dataset SHA-256: `ddfeee6bbf5324f9f64fd604e9d4bfc7bd2a45ce2896e7b968037af58de04791`

This activation is limited to research use. It is not exchange-verified, a strategy approval, campaign qualification, paper-session approval, production approval, or real-money authorization. `TRADING_MODE=paper`, `LIVE_TRADING_ENABLED=false`, and `BROKER_ADAPTER=disabled` remain mandatory.

## Activated scope

| Symbol | Rows | Coverage | Adjusted | Unadjusted |
|---|---:|---|---:|---:|
| GP | 6,089 | 2012-10-01 to 2026-01-22 | 3,045 | 3,044 |
| ACI | 6,009 | 2012-10-02 to 2026-01-22 | 3,005 | 3,004 |
| BRACBANK | 6,005 | 2012-10-02 to 2026-01-22 | 3,003 | 3,002 |

The 18,103 rows comprise 709 `tier_1_cross_source_confirmed` rows and 17,394 `tier_2_single_source_high_quality` rows. No tier outside the three authorized research tiers is permitted.

## Applied decisions

- Coverage-metadata adjusted and unadjusted series are primary. AmarStock and DSE Stocks are validation sources. Historical unknown-adjustment data remains fallback-only.
- GP is approved with all invalid, calendar, corporate-action, and conflict rows excluded.
- ACI is conditional; 2021-04-26 and all other held/rejected rows are excluded.
- BRACBANK is conditional; 2021-04-26, 2023-04-16, and all other held/rejected rows are excluded.
- DSEX is rejected in full and cannot be used as a benchmark. Buy-and-hold is the only planned comparator.
- No corporate action is verified: 938 records remain insufficient and 12 likely-but-unconfirmed. Affected rows remain excluded.
- The calendar is not authoritative. Only source-present dates are included; no trading day, holiday, or gap is inferred.
- All six conflicts retain blank reviewer decisions and operator status `hold_for_review`; no winner or averaged value was created.

Excluded ledger counts are: 240 mapping, 8 conflict, 68 calendar, 710 corporate action, and 712 invalid. The apparent conflict count includes both canonical and preserved held-candidate records; none is active.

## Audit and strategy boundary

Nine independent operator decisions were appended to canonical chain `c7aa6ed0-1288-417f-acbb-6ad4bfdd967c`; canonical event count advanced from 192 to 201 and verified valid immediately afterward. Each active row retains source row IDs, raw hashes, contributing sources, transformation version, approval decision, activation timestamp, and audit linkage.

The `ma_crossover@1.0.0` execution plan is prepared but **not authorized and not executed**. It binds the dataset/code/parameter hashes, exclusions, source hierarchy, unapproved fee and slippage sensitivity assumptions, buy-and-hold comparison, walk-forward and parameter-sensitivity designs, and sample-size assessment. A separate operator authorization is required before any research run.

The private activation evidence is retained under `reports/research_dataset_activation/gp-aci-bracbank-research-f24a48cb729e8a65/`. The portable report packager validated the artifact contract but its installed browser remained in fallback state during static-chart extraction; no HTML report is claimed. The JSON result, authorization record, execution plan, artifact input, and manifest remain the canonical evidence.
