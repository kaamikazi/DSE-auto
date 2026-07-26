# Cross-source validation

Rows are joined by symbol and trading date and classified as `exact_match`, `within_tolerance`, `material_conflict`, `missing_primary`, `missing_secondary`, `corporate_action_suspected`, `invalid_ohlc`, or `duplicate`. Unresolved review is represented by the run state. Prices are never averaged.

Each run emits a hashed JSON report, CSV discrepancy ledger, and HTML summary with symbol, date, and dataset quality scores. Material conflicts require a human decision supported by retained evidence.
