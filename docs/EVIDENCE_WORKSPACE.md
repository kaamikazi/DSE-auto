# Milestone 11 Evidence Workspace

The evidence workspace collects genuine DSE, broker, account, strategy-review, and market-data evidence without interpreting submission as approval. It is a review workspace, not an execution workflow.

## Cases and inbox

Sixteen idempotent collection cases cover market rules, calendar/hours, ticks, price limits, settlement, suspensions, corporate actions, broker fees, taxes/deductions, account statements, transactions, holdings, dividends/bonus shares, OHLCV, DSEX, and independent strategy/risk review.

Files are accepted in safe PDF, CSV, XLSX, image, text, and Markdown formats. Intake sanitizes filenames, validates type/signature, limits size, computes SHA-256, detects duplicate content, and keeps immutable raw evidence. Batch intake reports each accepted and rejected file independently.

The exact operator attestation is:

> I confirm these documents are described accurately, contain no credentials, and are submitted for review only; upload does not mean verification or approval.

Never upload broker passwords, PINs, OTPs, API keys, session cookies, or recovery codes.

## Extraction and review

CSV/XLSX and supported text files use deterministic extraction. Every claim retains its evidence ID, source location, original value, normalized interpretation, confidence, method, dates, and audit links. Image and scanned-PDF evidence is manual-transcription only unless deterministic text is available.

Review actions are accept, correct, reject, request a better source, mark conflicting, or mark obsolete. Accepting or correcting confirms extraction accuracy only. It never verifies the underlying document or approves a configuration.

## Conflicts and source hierarchy

The source hierarchy is configurable and visible. Official exchange and regulator publications rank above broker/account documents, licensed vendors, operator-attested files, informal pages, and social media. Rank does not auto-verify or auto-resolve a claim. Value, effective-date, hierarchy, and account-applicability differences are reported for human resolution.

## Decision assistants

Rule and fee assistants show the current draft, linked claims, source rank, effective dates, conflicts, missing evidence, conservative alternatives, and explicit item-level options. Fee views include deterministic examples for BDT 5,000, 10,000, 50,000, 100,000, and 500,000. They cannot approve or activate anything.

Portfolio statements are parsed into review-only drafts, reconciled with the previous draft, and reversible. They create no portfolio transactions, paper holdings, orders, or fills. Market datasets receive schema, date-range, missing-session, duplicate, outlier, corporate-action, timestamp-provenance, and quality reports; preview adds zero qualification days and performs no activation.

## Approval packs

Approval packs are independently scoped to rules, fees, risk limits, real datasets, `ma_crossover` promotion, or campaign creation. Each pack includes evidence hashes, source hierarchy, reviewed claims, conflicts, missing evidence, proposed values, conservative alternatives, reviewer independence, and consequences. Generation grants no approval and blanket approval is forbidden.

Initialize planned cases and generate fail-closed packs after migration:

```powershell
Set-Location 'E:\DSE AutoTrader'
$env:PYTHONPATH = "$PWD\backend"
.\backend\.venv\Scripts\python.exe .\scripts\initialize_evidence_workspace.py
```

The command aborts unless paper-only settings are active and the before/after counts prove that it created no campaign, session, order, fill, transaction, or promoted strategy. Historical test and prior-milestone records may remain preserved; they must not change.
