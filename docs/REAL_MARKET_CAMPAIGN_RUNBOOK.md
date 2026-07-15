# Real-Market Paper Campaign Runbook

Milestone 10 supports a 60-accepted-day campaign using reviewed real DSE data while every order and fill remains simulated. `TRADING_MODE=paper`, `LIVE_TRADING_ENABLED=false`, and `BROKER_ADAPTER=disabled` are mandatory.

Create a configuration with a 60-day qualification target, governed symbols and strategies, active rule/fee IDs, pessimistic fill model, DSEX benchmark, risk limits, reviewer assignments, and either a passing licensed provider certification or `allow_operator_attested: true`. Run `scripts\m10-operator.ps1 campaign-create --config <json>`.

The lifecycle is `configured -> awaiting_data -> ready -> active`, with fail-closed paths through `paused`, `degraded`, `reconciliation_required`, or `invalidated`. An operator must run data preview/activation, `premarket-check`, and then `session-start`. A real-market day is not qualifying until EOD evidence is complete and the human decision is `accepted`.

Synthetic, accelerated, historical-emulation, test-feed, and dry-run days are always excluded. The current counter remains `0/60` until genuine accepted days exist.
