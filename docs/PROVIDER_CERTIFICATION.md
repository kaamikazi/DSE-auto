# Provider Certification

Future adapters implement the vendor-neutral `DataAdapter` descriptor and health contract. `certify_adapter` produces an integrity-hashed report and persisted certification record.

Passing requires licensed operational use; required quote/history/DSEX/corporate-action capabilities; exchange-verified timestamps; approved-symbol coverage; valid/fresh quote schema; bounded latency; ordered, duplicate-free, repeatable history; DSEX history; corporate-action contract; and healthy reconnect checks. Failure of any check sets `activation_allowed=false`.

Test-only adapters deliberately fail the licensing gate even when their technical contract is complete. Real-market campaign creation requires a persisted passing certification ID. A certification must be rerun after schema, credentials, endpoint, licensing, SLA, timestamp, or material adapter changes.

Certification does not authorize live broker execution and does not prove profitability.
