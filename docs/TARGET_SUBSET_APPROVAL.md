# Target-subset research-activation approval pack

The final GP, ACI, BRACBANK, and DSEX review is an inactive decision package. Research activation remains `REJECTED / NOT GRANTED`, qualification remains `0/60`, and the permanent safety state remains paper trading with live trading disabled and the broker adapter disabled.

## Measured findings

- The 6,586 preserved `00DSEX` rows comprise 1,051 alternate-label candidates, 240 duplicate-alias candidates, and 5,295 unresolved rows. The unresolved population separates into 4,594 adjusted/unadjusted OHLC duplicates, 680 malformed OHLC rows, 2 single-date cross-grain representations, and 19 unknown cross-grain differences. No alias was approved.
- The 680 invalid DSEX rows contain 679 primary open/close range violations and one zero-index row; all raw values and lineage remain unchanged. A valid same-date counterpart exists for 170 rows, but each remains a recovery candidate requiring review rather than an automatic repair.
- All 231 scoped DSEX volume comparisons remain `field_not_comparable`. The official DSE data-service document distinguishes quantity, value, and trade-count fields, while its index table has no volume field. The stable approximate 100x ratio does not prove a conversion, so DSEX volume is excluded.
- Six conflicts remain separate: ACI on 2021-04-26; BRACBANK on 2021-04-26 and 2023-04-16; DSEX on 2021-04-26 and 2021-07-25; and GP's material 2021-04-26 volume disagreement. Every recommendation is `hold_for_review`.
- Corporate-action evidence remains 938 `insufficient_evidence` and 12 `likely_but_unconfirmed`; no issuer/date-specific official match was found in registered evidence. Observed calendar gaps and weekend rows remain separate from unapproved holiday and historical-weekend assumptions.

## Proposed inactive policy

Coverage-metadata adjusted and unadjusted grains are proposed as the broad primary research/validation sources for GP, ACI, and BRACBANK, with AmarStock and the DSE Stocks yearly CSV as limited cross-checks. Unknown-adjustment historical data remains fallback-only or excluded from adjusted/unadjusted primary roles. DSEX price hierarchy is not ready pending alias decisions; DSEX volume has no proposed primary source.

The versioned subset proposal contains 18,103 rows approvable only after human decisions, 240 held for mapping, 8 held for conflict, 68 held for calendar review, 710 held for corporate-action review, and 712 rejected invalid observations. These counts mix explicitly labelled canonical-candidate, held-candidate, and invalid-observation populations; the ledger preserves the grain of every row.

The generated pack is under `reports/target_subset_approval/approval_078e52bc0749d59761108799` and is intentionally ignored by Git. Its manifest hash is `6ba12d6ab0dcc60cf7166cf676242b2cb3169c21ea3696a257cfb12389a7cc78`. The Markdown, JSON, CSV, and validated self-contained HTML outputs are all hashed. Enhanced Chromium verification remained in the portable reader's fallback state, so the HTML uses the builder-validated semantic fallback rather than claiming enhanced-reader browser QA.
