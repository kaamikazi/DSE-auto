# Five-Symbol Conflict-Methodology Audit

> Historical methodology note: the table below was the comparison-correction
> stage and was not a final row-disposition equation. The subsequent
> [pilot final-disposition review](PILOT_FINAL_DISPOSITION_REVIEW.md) separates
> row and pair grains, includes invalid rows, splits same-source duplicate
> conflicts from genuine cross-source conflicts, and removes T1 status where
> derivation independence is unproven.

This work is a read-only methodology correction for `IDLC`, `LANKABAFIN`,
`BATBC`, `SQURPHARMA`, and `POWERGRID`. It grants no dataset or strategy
activation. Qualification remains **0/60**.

## Root cause

The preserved five-symbol baseline contains 36,665 of the former 87,191
conflicts. The old reconciler compared every non-exact record pair and therefore
treated 9,301 adjusted-versus-unadjusted comparisons and 27,359 known-versus-
unknown adjustment-grain comparisons as unresolved. One additional record was a
volume-only difference whose units are not registered. Only four records are
eligible same-grain OHLC disagreements. The adjusted/unadjusted Mendeley pair
also shares one raw archive, so duplicate logical provenance is a contributing
flag on the 9,301 cross-grain comparisons.

The corrected contract permits only date-aligned, high-confidence-mapped,
distinct-file, same-adjustment OHLC comparisons. Its relative tolerance is
0.1%. Exact duplicates collapse first while preserving raw-file hashes, source
row IDs, duplicate group IDs, every fingerprint, the representative row, and
the rationale. Conflicting same-source duplicates remain separate and held.
Volume, turnover/value, and trade counts remain ineligible until registered
units and aggregation semantics match. Every rejected pair retains reason codes.

## Measured result

| Symbol | Raw | Logical | Exact collapsed | Ineligible pairs | Genuine | Invalid | T1 | T2 | T3 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| IDLC | 12,708 | 12,232 | 184 | 8,827 | 2 | 38 | 239 | 6,002 | 5,971 |
| LANKABAFIN | 11,043 | 10,593 | 180 | 8,861 | 1 | 17 | 239 | 6,026 | 4,310 |
| BATBC | 12,984 | 12,531 | 184 | 8,854 | 0 | 15 | 240 | 6,015 | 6,262 |
| SQURPHARMA | 13,115 | 12,660 | 185 | 8,949 | 0 | 15 | 241 | 6,076 | 6,328 |
| POWERGRID | 10,991 | 10,514 | 175 | 8,788 | 1 | 47 | 240 | 5,968 | 4,285 |

Aggregate inactive tiers are T1 1,199, T2 30,087, and T3 27,156. Another 72
logical rows are held under the prescribed conflict status: four eligible
cross-source conflicts plus 68 conflicting same-source duplicate keys. Sixteen
logical rows are held for material long-gap lifecycle review. No corporate-
action row is held as evidence-supported because registered support is absent.

## Corporate actions and lifecycle

The former 1,999 action candidates resolve to 906 ordinary movements, 1,056
missing-session discontinuities, 12 long gaps, 19 adjustment divergences, and
six duplicate-source divergences. Fifteen have multiple heuristic signals, but
zero have registered supporting evidence; none is approved. Large movement
alone is `insufficient_evidence`.

All five symbols remain `lifecycle_evidence_pending`. Official listing, first-
trade, delisting, suspension/resumption, rename, and instrument evidence was not
found in the already-registered material. A conservative research window of
2012-10-01 through 2026-01-22 can be proposed from accepted known-adjustment
observations, but it is not a listing-date claim.

## Source overlap and human queue

DSEStocks/Mendeley unadjusted overlap spans 2021-01-03 through 2021-12-30:
238-239 comparisons per symbol with 99.58%-100% agreement. AmarStock supplies
one adjusted and one unadjusted comparison on 2023-04-16. These are distinct
registered files, but derivation independence is not proven. Mendeley is the
only usable long-coverage source for most periods, so concentration remains a
binding caveat even though registered quality and coverage justify inactive T2
labelling.

The human queue is nine items: four genuine OHLC conflicts and one material
lifecycle decision per symbol. It excludes cross-grain pairs, unit mismatches,
exact duplicates, same-source duplicates, and unsupported action heuristics.
No symbol is ready for activation review.

The authoritative evidence is under
`reports/pilot_conflict_methodology/pilot_e5003bc95233252838ac7307/`, with
manifest hash
`1649bc90bcf6dae3b4f9ce22508775d010f30bc024d74521a6d4e33138156c38`.
The JSON, CSV, and Markdown outputs are authoritative. A single portable-HTML
attempt failed closed because the shared reader remained in fallback state.
