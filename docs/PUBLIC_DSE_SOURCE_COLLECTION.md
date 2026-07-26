# Public DSE Source Collection

Collection completed on 2026-07-26 from the bounded public sources in
`config/public_dse_source_catalog.json`. Raw files are retained in ignored local evidence storage and
must not be committed or redistributed. A source's official domain establishes provenance, not
authenticity, current applicability, or approval.

## Collection outcome

- 16 source attempts: 15 downloaded files and 1 `manually_required` item.
- Five dataset artifacts were registered with five review-only schema previews. All remain
  `registered`; all preview runs remain `review_required`.
- Ten evidence artifacts were registered, including nine official PDFs and one third-party
  instrument list.
- 62 deterministic draft claims were extracted from official PDFs. Every claim remains
  `under_review` and retains its source file, URL, and page reference.
- Qualification remains **0/60**. No rule, fee, risk limit, dataset, strategy, campaign, session,
  proposal, order, or fill was activated or created by collection.

## Dataset quality findings

- Mendeley `5mww8rb9td` states 1,684,249 rows and more than 700 companies. The retained CSV preview
  found 1,523,921 rows, 529 distinct symbols, 68,739 duplicate symbol/date rows, and 22,519 rows
  failing basic OHLCV validity checks. This discrepancy requires author/source review.
- The adjusted/unadjusted Mendeley archive contains 977 CSV members and expands to 222,627,258
  bytes. Only bounded schema inspection was performed; no member was activated.
- The DSE Stocks 2021 file is headerless. Its preview inferred the documented seven-column layout,
  found 94,292 rows and 412 symbols, and left the mapping under review.
- Mendeley versus DSE Stocks produced 75,134 overlapping 2021 symbol/date rows: 57,250 exact matches
  and 17,884 material conflicts. Values were not averaged.
- AmarStock adjusted versus unadjusted produced 321 overlapping rows: 273 exact matches and 48
  material conflicts; the adjusted file also contained 14 additional rows.
- Mendeley states that its adjusted/unadjusted data were curated from public portals including
  AmarStock. Those two datasets are therefore not independent corroboration.

Detailed JSON, CSV, and Markdown discrepancy artifacts, file-hash verification, registry IDs, and
audit links are retained under `reports/evidence_workspace/public_sources/`.

## Human-review checklist

- [ ] Trading days: identify the current official instrument and effective date.
- [ ] Trading hours: confirm ordinary, Ramadan, and special-session hours.
- [ ] Market phases: confirm phase names, sequence, and order permissions.
- [ ] Holidays: obtain the current official DSE calendar/holiday publication (manual download).
- [ ] Tick sizes: verify current tables by security/category and amendment history.
- [ ] Price bands: verify circuit-breaker bands, exceptions, and effective dates.
- [ ] Settlement: reconcile BSEC rules, CDBL bye-laws, DSE rules, and current practice.
- [ ] Suspensions: confirm authority, notice semantics, and resumption handling.
- [ ] Corporate actions: confirm record-date, entitlement, adjustment, and settlement rules.
- [ ] Short selling: reconcile BSEC and DSE documents and confirm whether rules are currently active.
- [ ] Leverage: verify current margin/non-margin directives and account applicability.
- [ ] Order expiry: confirm validity types, session boundaries, and cancellation rules.
- [ ] Fees and charges: obtain licensed/current schedules and tax evidence; do not infer from rules.
- [ ] Scanned/weak-text PDFs: manually transcribe and page-check the clearing/settlement and EOD
  documents where deterministic extraction produced no claim.
- [ ] Dataset licenses: obtain explicit redistribution/automated-use terms for DSE Stocks,
  AmarStock, DSE data products, and underlying Mendeley market data.
