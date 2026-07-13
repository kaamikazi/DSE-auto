# Incident Response

Tracked types cover provider outage, stale data, timestamp trust, reconciliation, audit, scheduler, EOD, drawdown, backup, database, notification, and restart failures.

States are `open`, `acknowledged`, `mitigated`, `resolved`, and `accepted_risk`. Records include severity, owner, timestamps, cause, evidence, remediation, campaign, and linked audit events. Critical incidents use Telegram when configured and console fallback locally.

Response order:

1. Fail closed and stop new approvals.
2. Preserve raw data, audit, logs, and backups.
3. Assign ownership and document containment.
4. Reconcile and verify audit before mitigation.
5. Resolve with cause/remediation, or explicitly accept risk without unsafe resumption.
