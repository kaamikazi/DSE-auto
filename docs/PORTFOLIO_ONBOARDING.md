# Portfolio Onboarding

Status: **verified by automated isolated tests**; no real broker access and no actual real portfolio file was activated in this run.

Reference-labelled imports support preview, validation, SHA-256 batch identity, duplicate rejection, reversible activation, preserved source records, and canonical audit events. Unknown credential columns fail validation and are not stored. Activation creates no order and submits nothing to a broker.

Paper reconciliation now includes only unlabeled legacy paper transactions plus `paper` and `simulation` labels. A `reference` import is excluded from paper cash and can be viewed separately with the account-label filter. Tests verified reference holdings, an empty paper view, unchanged healthy paper reconciliation, zero orders, duplicate detection, reversal, and valid audit. The comparison engine remains available for reference-imported, buy-and-hold, DSEX, strategy, and combined-paper series; no portfolio is labelled profitable or best.

