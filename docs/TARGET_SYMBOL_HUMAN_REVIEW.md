# Target-symbol human review result

This review is inactive and limited to GP, ACI, BRACBANK, and DSEX. It does not approve a
mapping or source hierarchy, activate a dataset or policy, run a strategy, promote a strategy,
start a campaign/session, or create a proposal, order, transaction, or fill. Qualification
remains 0/60.

The final ignored evidence pack is
`reports/target_symbol_human_review/review_655ec146704dc08b19b67b9a`, generated from Git
`a1ed18d84dfd44fab8fedd9d5ce3f8da8c3de83f`. Its manifest hash is
`145f87b286cb34b3de4e6a8809c511ab7acc56a8d38d5be0086bb7e886c4bc8a`;
all 21 content hashes independently matched.

## Source recommendations

For GP, ACI, and BRACBANK, the Mendeley coverage-metadata adjusted and unadjusted grains are
recommended as `primary_candidate`, subject to human approval. DSE Stocks 2021 and AmarStock
unadjusted are `secondary_validation`; AmarStock adjusted is `adjustment_reference`; the
Mendeley historical dataset with unknown adjustment semantics is `fallback_only`. These are
recommendations, not final selections. All five `00DSEX`-contributing grains remain
`unresolved`; the literal-DSEX historical series remains `fallback_only` until mapping,
duplicate, unit, and license review completes.

## DSEX mapping

All 6,586 unresolved rows use raw symbol `00DSEX`. Row-level review classified 1,051 as
`alternate_dsex_label`, 240 as same-source `duplicate_alias`, and 5,295 as `unresolved`.
There are 5,906 OHLCV-valid and 680 preserved invalid rows. No registered official document
explicitly proves the alias, so no row was merged or approved.

## Conflicts and units

The legacy 690/99/229 volume/rounding/unexplained counts are global. Within target scope:

- 231 DSEX volume cases show a stable 100x ratio between the coverage-metadata and DSE Stocks
  2021 sources (98.44 minimum, 100 median/maximum). The statistical factor has medium
  confidence, but unit semantics are unknown; automatic rescaling is forbidden.
- The sole target legacy-rounding row is GP on 2021-04-26. Its price difference is within
  0.1%, but volume differs 15.05%, so it is a `material_discrepancy`, not harmless rounding.
- Five unexplained price conflicts remain held: ACI 2021-04-26; BRACBANK 2021-04-26 and
  2023-04-16; DSEX 2021-04-26 and 2021-07-25. The other 224 global cases are outside scope and
  unchanged.

## Corporate actions and calendar

The six adjustment divergences occur outside the target symbols. Target review retained 950
action candidates: 938 have insufficient evidence and 12 are suspension/resumption candidates;
none is confirmed. Counts by symbol are ACI 388, BRACBANK 384, and GP 178.

Observed long gaps are ACI 16, BRACBANK 11, DSEX 7, and GP 18. All-source missing expected
days are 163, 165, 37, and 307 respectively. Friday/Saturday flags use the current convention;
historical weekend regimes, holidays, suspensions, and collection failures remain unresolved.
The calendar is not authoritative.

## Readiness and required approvals

- GP: `source_approval_required`.
- ACI and BRACBANK: `conflict_review_required` plus source approval.
- DSEX: `mapping_review_required`, conflict review, and source approval.

Human approval is required for the DSEX alias and invalid-row treatment; per-grain source roles;
volume semantics/conversion; six target conflict decisions including the GP material volume case;
issuer/ex-date corporate-action evidence; an authoritative calendar; and each provisional target
policy. Operational and activation counts had zero delta, the canonical audit remained valid,
and safety remained paper/false/disabled.
