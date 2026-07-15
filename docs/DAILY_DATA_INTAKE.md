# Daily Data Intake

Accepted input is either a certified licensed exchange-timestamped provider or reviewed operator-attested files. Operator files retain `timestamp_trust=operator_attested`; they are never promoted to `exchange_verified`.

Supported kinds are quotes, OHLCV, DSEX, corporate actions, price-sensitive news references, suspension status, and trading-status changes. Preview validates schema, market date, symbols, timestamps, duplicates, source description, SHA-256, and the exact attestation:

> I confirm this file represents the stated DSE market date and source, and I understand it is operator-attested rather than exchange-verified.

Use `data-import-preview`, inspect every error and warning, then use `data-activate`. Activation preserves raw bytes, provenance, hash, campaign/date linkage, and an audit event. `data-rollback` reverses the activation without deleting raw evidence. Never include credentials, scrape around restrictions, or bypass TLS verification.
