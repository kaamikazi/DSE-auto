# Data Quality Evidence

Continuous observations measure quote age, source latency, ingestion latency, activation latency, duplicate and missing-update rates, provider disagreement, out-of-order events, stale intervals, symbol coverage, and market-session coverage. Timestamp trust is preserved with every observation.

Daily, weekly, and campaign reports are persisted and exported as JSON, CSV, and SVG. Each report has a SHA-256 integrity hash and a pass/fail result. The JSON explicitly states whether strategy results may be displayed.

Backtests attach inline data-quality counts and a hash. Campaign API responses withhold cumulative strategy results until a linked passing quality report exists. Quality evidence validates input handling; it does not prove the vendor's data is correct, licensed, complete, or exchange-grade.

Default pass criteria are fail-closed: data must exist; required symbols must be covered; timestamps must have approved trust; no record may be stale; and duplicates/out-of-order events must be zero. Production thresholds must be approved for the actual feed and DSE session schedule.
