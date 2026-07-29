# Pilot Final-Disposition Review

This read-only reconciliation covers `IDLC`, `LANKABAFIN`, `BATBC`,
`SQURPHARMA`, and `POWERGRID`. It activates no data or policy, executes no
strategy, and leaves qualification at **0/60**. The proposed activation policy
is **REJECTED / NOT GRANTED**.

## Why the former table did not balance

The prior table displayed raw rows, logical candidate rows, pair-level
comparisons, invalid raw rows, and diagnostic holds together without a common
denominator. Its logical count excluded 132 invalid rows. It also grouped 68
conflicting same-source duplicate keys with four genuine cross-source conflict
rows. Finally, 1,199 distinct-file agreements were labelled T1 even though
source-derivation independence was explicitly unproven.

The corrected model keeps these grains separate: 60,841 raw source rows;
58,662 logical deduplicated rows; 908 duplicate groups (840 exact and 68
conflicting); 45,482 comparison pairs; 44,279 ineligible pairs; and four
genuine conflict pairs. Pair counts and diagnostic-flag counts never enter the
row-disposition equation.

## Exact row reconciliation

| Symbol | Logical | T1 | T2 | T3 | Genuine | Lifecycle | Invalid | Duplicate conflict |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| IDLC | 12,270 | 0 | 6,241 | 5,971 | 2 | 5 | 38 | 13 |
| LANKABAFIN | 10,610 | 0 | 6,265 | 4,310 | 1 | 4 | 17 | 13 |
| BATBC | 12,546 | 0 | 6,255 | 6,262 | 0 | 0 | 15 | 14 |
| SQURPHARMA | 12,675 | 0 | 6,317 | 6,328 | 0 | 1 | 15 | 14 |
| POWERGRID | 10,561 | 0 | 6,208 | 4,285 | 1 | 6 | 47 | 14 |
| **Combined** | **58,662** | **0** | **31,286** | **27,156** | **4** | **16** | **132** | **68** |

Corporate-action holds, mapping holds, and other rejections are zero. Every
per-symbol equation and the combined equation balance exactly.

## Tier contract

T1 requires complete lineage, valid OHLC, known adjustment grain,
high-confidence mapping, proven independently eligible cross-source agreement,
and no conflict or hold. Because independence is not proven, T1 is zero.

T2 requires the same lineage, OHLC, adjustment, and mapping controls; a
high-quality primary source; unavailable independent validation; and no
conflict or hold. T3 requires complete lineage and no structural OHLC failure,
but at least one exact weakness code. T3 is ineligible by default.

All 27,156 T3 rows carry four diagnostic flags:
`adjustment_documentation_incomplete`, `provenance_weaker`,
`timestamp_trust_weaker`, and `source_quality_below_tier2`. Counts are per flag,
so they must not be summed as rows.

## Decisions and readiness

Four independent conflict records cover IDLC (2021-04-26 and 2023-04-16),
LANKABAFIN (2021-04-26), and POWERGRID (2021-04-26). Five independent lifecycle
records cover one decision per pilot symbol. All reviewer and operator decisions
are blank. Every symbol, including priority reviews BATBC and SQURPHARMA, is
`human_decision_required`; none is ready or activated.

The proposed inactive policy would make T1/T2 eligible by default, while T3,
every held status, and every rejected status remain ineligible. Any T3 exception
would require separate explicit authorization by reason category.

## Evidence

The authoritative pack is
`reports/pilot_final_disposition/pilot_final_7e9fccd005d9225089a70dbc/`.
Its manifest hash is
`bcc52fbd94bc65a54ac3c42a6240b40d4c664dfc9db809db88449cc253b4efd1`.
It links to prior manifest
`1649bc90bcf6dae3b4f9ce22508775d010f30bc024d74521a6d4e33138156c38`.
Canonical audit event `67e4fa77-4755-45c8-82bd-d622eb79bcdd` records the
reconciliation. Markdown, JSON, CSV, and self-contained HTML are provided, and
every artifact is SHA-256 hashed in the manifest.

