# Approved Data Import Attestation

Quote, OHLCV, and DSEX CSV batches support preview, duplicate and symbol checks, timezone-aware timestamp validation, row validation, SHA-256 hashing, immutable raw retention, activation, and rollback.

The operator must confirm exactly:

> I confirm this file represents the stated market date and source.

Activation requires a separate written approval. Raw files are stored by content hash under `data/raw_imports`; rollback removes activated rows but retains original evidence.

Imported timestamps are always `operator_attested`, never `exchange_verified`. Naive timestamps, mismatched dates, unknown or out-of-universe symbols, malformed OHLC, negative values, and duplicate hashes fail closed.

Templates are downloadable from `/api/v1/data-imports/templates/quote`, `/ohlcv`, and `/dsex`.
