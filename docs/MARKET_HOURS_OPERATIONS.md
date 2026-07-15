# Market-Hours Paper Operations

Only an active session that passed pre-market may ingest activated market updates. Retain provenance, reject stale or missing timestamps, run only governed strategies, and record skipped signals and risk rejections. Existing one-time human paper approvals remain required; AI cannot approve.

All proposals, partial fills, fills, fees, cash, positions, drawdown, concentration, liquidity, data quality, and infrastructure health remain paper records. Threshold failures create incidents and may degrade, pause, require reconciliation, or emergency-stop the session. No code path sends a real order.

Use `campaign-state <id> pause|resume --reason "..."` and `emergency-stop --reason "..."`. Do not resume until the incident and reconciliation evidence is reviewed.
