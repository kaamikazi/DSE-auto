# GP, ACI, BRACBANK, and DSEX research review policy

This workflow creates an inactive, human-reviewable research subset. It cannot activate a
dataset, rule, fee, limit, strategy, campaign, session, proposal, order, transaction, or fill.
Qualification remains 0/60.

## Comparison eligibility

Adjusted and unadjusted series are different truth fields and are never compared as
equivalent prices. Symbol/date mapping must be sufficiently certain. Price OHLC comparisons
use a 0.1% relative tolerance; volume uses a separate 2% tolerance. Duplicate-source conflict,
scale mismatch, date misalignment, uncertain symbol mapping, and corporate-action periods are
reported separately rather than averaged away.

Corporate-action detection requires stronger evidence than an adjusted/unadjusted divergence.
Without an official announcement, ex/record date, adjustment-factor discontinuity, stable
proportional OHLC change, volume continuity, or cross-source confirmation, a row is labeled
`adjustment_divergence`, `discontinuity_for_review`, or `insufficient_evidence`. The workflow
does not approve bonus shares, splits, rights issues, dividends, or suspension/resumption.

## Candidate subset

A candidate row requires an approved mapping, valid date and OHLC, known source and adjustment
state, no unresolved eligible duplicate/source conflict, valid comparison eligibility,
complete immutable lineage, and an explicit quality status. Allowed inactive tiers are:

- `tier_1_cross_source_confirmed`;
- `tier_2_single_high_quality_source`;
- `tier_3_low_confidence_research_only`;
- `held_for_manual_review`;
- `rejected`.

Source selection is explicit and adjustment-specific. Each retained row carries its source
dataset ID, file hash, raw row identifier, source URL where available, original values, and
transformation version/reason. Human samples cover deterministic valid rows, conflicts,
largest price/volume discrepancies, suspected actions, first/last dates, long gaps, and
duplicates. No tier is automatically activated.
