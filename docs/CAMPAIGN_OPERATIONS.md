# Sustained Paper Campaign Operations

Validation campaigns are persistent evidence boundaries. Each records approved symbols and versioned strategies, date range, starting capital, risk and data policies, timestamp-trust requirement, fill model, DSEX benchmark, notes, rule-set ID, and fee-profile ID.

States are `configured`, `active`, `paused`, `degraded`, `reconciliation_required`, `completed`, `failed`, and `archived`. Only one controlling campaign may use a paper account. Rule versions cannot change while a campaign controls the account. Sessions, market bars, signals, orders, transactions, imports, incidents, and reports carry campaign identity.

Operator flow:

1. Approve a rule set and fee profile.
2. Register strategies, supply all evidence, and explicitly promote each through research, candidate, and paper-active states.
3. Configure and activate a campaign with an operator reason.
4. Run one campaign day per configured trading date.
5. Complete EOD before the next day. Recovery leaves the campaign paused for review.
6. Complete, review, and archive the immutable record.

Run commands from `backend` with `PYTHONPATH` set to that directory:

```powershell
..\scripts\operator.py campaign status <campaign-id>
..\scripts\operator.py campaign activate <campaign-id> --reason "Operator starts approved paper campaign"
..\scripts\operator.py campaign-day start <campaign-id> --date 2026-07-13
..\scripts\operator.py campaign-day eod <campaign-id> --date 2026-07-13
```

Campaign results are evidence, never a profitability claim or live-execution authorization.
