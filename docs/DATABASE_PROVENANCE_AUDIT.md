# Database provenance audit result

Generated 2026-07-28 from Git `14d84ff652e542d045c4aa312fe1878b64e1341d`.
The complete ignored evidence pack is
`reports/database_provenance_review/review_e234e547d15842f5e8d828af`; its manifest hash is
`4666aeb442793cf8dc7a14e04ae3e063eeaec4a72307eaee889f203d97387b04`.
All 14 content hashes independently matched the manifest.

## Operational provenance

The source of truth is `E:\DSE AutoTrader\backend\data\dse_autotrader.db` (SQLite,
SHA-256 `14b9f5f631b66ffd6a733436b91a55827e23de227af3959b2ed4d1d3fcef7823`,
Alembic `0012`, active audit chain `c7aa6ed0-1288-417f-acbb-6ad4bfdd967c`, 192
canonical events, 398 legacy archived events). The inventory classified all discovered
database artifacts; none remain unknown. PostgreSQL development/test identities were recorded
as unavailable because Docker was offline and were not inferred from historical evidence.

The preserved historical ledger has 3 campaigns, 5 sessions, 5 orders, and 2 transactions:
12 records are `synthetic_simulation` and 3 are `imported_data_validation`. No record is
unknown or suspicious. Evidence fields prove zero real broker connections and zero real order
submissions. The originating Git commit was not embedded in these records; nearest prior
commits are retained only as non-proof context.

## Detector and conflict findings

The old detector classified 105,995 rows as probable bonus adjustments using close-to-close
ratios without official evidence. Conservative reclassification produced 6
`adjustment_divergence`, 3,019 `discontinuity_for_review`, and 114,865
`insufficient_evidence`; no corporate action was approved.

Of 1,058,979 outside-tolerance comparisons, 1,057,961 were ineligible adjusted-versus-
unadjusted comparisons. The remaining 1,018 eligible conflicts comprise 690 volume-unit
mismatches, 99 rounding differences, and 229 unexplained conflicts.

## Inactive target subset

| Symbol | Coverage | Candidates | Held groups | Invalid rows | Conflicting rows | Unresolved mappings |
|---|---|---:|---:|---:|---:|---:|
| GP | 2009-01-12 to 2026-01-22 | 6,292 | 3,625 | 4 | 2 | 0 |
| ACI | 1999-01-02 to 2026-01-22 | 6,285 | 6,388 | 21 | 2 | 0 |
| BRACBANK | 2007-01-02 to 2026-01-22 | 6,308 | 4,295 | 5 | 4 | 0 |
| DSEX | 2012-10-01 to 2026-01-22 | 240 | 8,073 | 682 | 0 | 6,586 |

The 19,125 inactive candidates comprise 718 tier 1, 18,167 tier 2, and 240 tier 3 rows.
Another 22,377 groups are held for invalid/unknown adjustment or mapping review, plus 4 for
eligible source conflict. Human decisions remain required for DSEX mapping, adjusted and
unadjusted source hierarchy, conflicts, units/licensing, corporate-action evidence, the DSE
calendar, legacy wording, and future embedded application versioning. Operational and
activation counts had zero delta; qualification remains 0/60.
