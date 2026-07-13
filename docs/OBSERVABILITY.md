# Local Observability

`GET /api/v1/metrics` and `/operations/summary` expose local health without secrets or positions. They include scheduler lag, quote age, ingestion latency, strategy runtime, order latency, audit-write latency, database health, queue depth, failure counts, and unresolved incidents.

Audit and scheduler latencies persist as structured metrics. Quote and order latencies derive from local records. Missing evidence is `null`, never healthy by assumption.

These endpoints assume localhost isolation. Wider deployment requires authenticated proxy controls, TLS, access logging, and independent monitoring.
