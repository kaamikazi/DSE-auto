# Canonical DSE research dataset review

This workflow builds an **inactive, human-review-only** historical OHLCV candidate from the
registered public research sources. It never writes normalized application bars, activates a
dataset, establishes source truth, approves evidence, or changes trading state. Qualification
remains `0/60`.

Run from `backend/` with the permanent safety settings in force:

```powershell
python ..\scripts\build_canonical_research_candidate.py
```

The deterministic run ID is derived from the source hashes, transformation version, and configured
tolerance. Local outputs are intentionally ignored under
`reports/research_data_quality/canonical_candidate_<run-id>/`; `manifest.json` records every file's
size and SHA-256, the immutable raw hashes, audit linkage, and the before/after operational-state
comparison.

## Quality policy proposed for human approval

- Keep adjusted, unadjusted, and unknown-adjustment series separate.
- Reject structural OHLCV, date, and symbol invalidity while retaining rejected evidence.
- Collapse only exact same-source duplicates and retain all original row identifiers.
- Exclude conflicting same-grain values; never average or silently prefer a source.
- Hold uncertain symbol mappings and corporate-action candidates for review.
- Treat third-party timestamps as `unknown` or `provider_asserted`, never `exchange_verified`.
- Keep every output row `pending_human_approval`; no score establishes truth.

## Measured result

Run `0a834213759f5a79` inventoried six logical datasets backed by five registered dataset files.
Its final canonical manifest content hash is
`7488b1badc567b006a313bf9739f11fdf1f3ee16d25f29a521ce5f234c73d30c`.
The largest source contained 1,523,921 rows and 529 symbols, versus publisher statements of
1,684,249 rows and more than 700 instruments. This discrepancy is unresolved and neither value is
treated as authoritative. The run materialized 3,569,387 candidate rows, retained 24,925 rows with
one or more raw validity classifications, recorded 64,622 duplicate groups, 3,028,122 pairwise
source comparisons, and queued 117,890 possible corporate actions.

All four initial symbols (`ACI`, `BRACBANK`, `DSEX`, and `GP`) remain `cleaning_required` because
invalidity, duplicates, conflicts, calendar gaps, or action candidates remain. The observed calendar
is descriptive only; Friday/Saturday heuristics and gaps require comparison with approved official
calendar evidence.

The operational database already contained historical campaigns, sessions, orders, fills, and
promoted strategy registrations before this task. The manifest proves their counts did not change.
It also proves zero active research bars, zero activated datasets, zero non-review previews, and zero
approved rule claims both before and after generation. That is a zero-delta assertion, not a claim
that historical operational records do not exist.

## Required human decisions

Review the publisher/file-count mismatch, exact-versus-conflicting duplicates, adjustment semantics,
symbol aliases (especially `00DSEX`), licensing, authoritative holidays and market dates, every
corporate-action candidate, and all unresolved cross-source differences before considering research
activation. This pack is not real-market evidence and does not authorize trading.
