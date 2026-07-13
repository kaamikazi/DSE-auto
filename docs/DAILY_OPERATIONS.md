# Daily Operations

The scheduler uses non-overlapping persisted jobs. Dhaka pre-market runs at 09:45, campaign EOD at 17:10, and drift detection every 30 minutes. Quote, DSEX, signal, news, reconciliation, snapshot, report, and backfill jobs continue.

Pre-market fails closed unless the canonical audit chain, recent backup, approved activated import, required timestamp trust, calendar, scheduler, emergency-stop state, account reconciliation, campaign, strategy governance, locked rule/fee versions, and paper-only settings pass.

During market hours, campaign-scoped jobs record provenance, skipped signals, risk decisions, proposals, rejections, fills, partial fills, drawdown, and incidents. Uncertainty never becomes approval.

EOD expires campaign orders, reconciles, snapshots, verifies audit, creates a backup, writes daily evidence, and completes the session. Reconciliation, audit, or backup failure blocks completion and opens an incident.

Missed EOD moves the campaign to `reconciliation_required`. Recovery reconciles, backs up, creates evidence, and leaves the campaign `paused`; an operator must review and resume. Startup detects incomplete prior days and opens an unexpected-restart incident.
