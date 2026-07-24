# Evidence Decision Workflows

## Rule item

1. Collect a genuine current source and preserve its hash.
2. Extract claims deterministically or transcribe them manually with exact source locations.
3. Review extraction accuracy.
4. Resolve conflicts and effective-date/account-scope differences.
5. Review the item-specific decision assistant and conservative fallback.
6. Generate only the `rules` approval pack.
7. Obtain explicit human authorization for the individual item through the existing governed approval path.

Pack generation, extraction acceptance, and case completion are not authorization.

## Fee item

Repeat the same workflow using an official account-specific broker schedule where possible. Confirm buy/sell applicability, percentage/flat/minimum charges, taxes, regulatory deductions, effective dates, and the deterministic BDT examples. Fees remain inactive until separately approved.

## Portfolio statement

Use a credential-free CSV/XLSX statement. Preview validates its hash, rejects duplicates and credential columns, extracts holdings/cash/transactions/dividends/bonus shares into a draft, and compares quantities with the prior draft. Review or reverse the draft. No parsed row is written to the operational portfolio ledger.

## Market dataset

Retain the original dataset evidence, then preview it with honest timestamp provenance. Review missing sessions, duplicates, outliers, symbol coverage, corporate actions, and source timestamps. A preview is not research approval, does not qualify a campaign day, and is not real-market evidence by itself.

## Completeness

The tracker reports exact states—missing, document received, extracted, reviewed, conflicting, decision ready, approved, rejected, or expired—for 16 rule items, 12 fee items, 12 risk items, dataset quality, independent review, strategy promotion, and campaign creation. Missing or conflicting items remain blocked.

