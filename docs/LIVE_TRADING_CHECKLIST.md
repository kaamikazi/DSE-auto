# Live Trading Checklist

Milestone 3 remains paper-only. Real money stays blocked until provider timestamp reliability, long-run evidence, reconciliation, fill assumptions, calendar governance, restore drills and soak failures receive independent review. Passing this checklist does not enable a broker adapter.

Milestone 5 recovered canonical audit integrity and completed one imported-data paper session. Live trading is still blocked because public DSE acquisition lacks trustworthy exchange timestamps, bdfinance is unavailable, and sustained multi-week operational evidence has not been produced.

All items require written sign-off:

- At least 60 DSE trading days of representative paper results
- No unresolved critical defects
- Official broker API documentation and explicit broker permission
- Successful holdings/cash/open-order reconciliation tests
- Duplicate-order and restart-recovery tests
- Kill-switch and stale/conflicting-data tests
- Security review, secret rotation and audit-storage durability
- Small-capital pilot with strict caps
- Manual approval for every pilot order
- Written acceptance of financial and operational risk

Milestone 1 cannot be enabled for live trading even if these boxes are checked; code and configuration changes plus a formal release are required.

## Milestone 6 blockers

- [ ] Several weeks of uninterrupted independently reviewed paper campaigns
- [ ] Current authoritative verification for every active DSE rule
- [ ] Binding broker/account fee and tax verification
- [ ] Contractually reliable exchange-timestamped quotes
- [ ] Resolution and independent review of critical/high incidents
- [ ] Repeated restore, missed-EOD, restart, audit, and reconciliation drills
- [ ] Robust out-of-sample strategy evidence without short-campaign profitability claims
- [ ] Production-grade database, distributed scheduling, independent alerts, monitoring, secrets, and disaster recovery

Milestone 6 does not enable broker execution. `TRADING_MODE=paper`, `LIVE_TRADING_ENABLED=false`, and `BROKER_ADAPTER=disabled` remain mandatory.

## Milestone 7 blockers

- [ ] Start and verify PostgreSQL 16 and Redis 7 with the Docker integration suite
- [ ] Complete hash-matched operational SQLite-to-PostgreSQL migration and rollback rehearsal
- [ ] Pass PostgreSQL and Redis restart phases in the distributed 30-day harness
- [ ] Restore a PostgreSQL backup on a clean machine and independently review measured RPO/RTO
- [ ] Obtain a licensed, contractually reliable DSE feed with trustworthy exchange timestamps
- [ ] Complete at least 60 real representative paper days, each with accepted independent review
- [ ] Resolve every rejected/rerun day and every critical incident; do not count them silently
- [ ] Independently validate risk thresholds, DSE rules, fees, taxes, liquidity and execution assumptions
- [ ] Add a hashed Python production lock and Python dependency advisory scanning
- [ ] Establish independent monitoring/alerting, secret management, encrypted backups and access review
- [ ] Obtain official broker API documentation, permission, legal/compliance review and a separately designed live release

Milestone 7 remains production-like paper infrastructure only. No checklist completion can activate the current disabled broker adapter.
