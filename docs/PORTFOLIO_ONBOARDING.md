# Portfolio Onboarding

Status: **verified by automated isolated tests**; no real broker access and no actual real portfolio file was activated in this run.

Reference-labelled imports support preview, validation, SHA-256 batch identity, duplicate rejection, reversible activation, preserved source records, and canonical audit events. Unknown credential columns fail validation and are not stored. Activation creates no order and submits nothing to a broker.

Paper reconciliation now includes only unlabeled legacy paper transactions plus `paper` and `simulation` labels. A `reference` import is excluded from paper cash and can be viewed separately with the account-label filter. Tests verified reference holdings, an empty paper view, unchanged healthy paper reconciliation, zero orders, duplicate detection, reversal, and valid audit. The comparison engine remains available for reference-imported, buy-and-hold, DSEX, strategy, and combined-paper series; no portfolio is labelled profitable or best.
# Reference Portfolio Onboarding

Use `portfolio-preview` before `portfolio-activate`. Supply a reviewed CSV, statement date, source-document description, and the exact portfolio attestation shown by the CLI. Supported records include broker/account label, symbol, quantity, acquisition cost/date when known, cash balance, dividends, bonus shares, and realized transactions.

The workflow hashes the batch, detects duplicates, rejects paper/simulation account labels, and rejects credential, password, PIN, and OTP columns. Activation is reversible, audited, and produces no order. Imported holdings and cash remain read-only and separate from the `paper` account; reversal appends compensating records rather than deleting evidence.

Comparisons may show the imported portfolio beside paper campaign, DSEX, buy-and-hold, and governed strategy results. They do not generate recommendations or real orders. No broker login, API, credential, or execution access exists.
