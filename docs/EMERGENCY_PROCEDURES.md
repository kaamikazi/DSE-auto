# Emergency Procedures

1. Trigger `POST /api/v1/risk/emergency-stop` or stop the backend process.
2. Preserve database, logs and audit records; do not delete or rewrite them.
3. Record time, operator, observed data/order/account state and reason.
4. Compare provider data and reconcile paper cash, holdings and active orders.
5. Verify the audit hash chain and resolve root cause.
6. Resume only through `/risk/resume`; it fails to `reconciliation_required` on inconsistencies.

For suspected secret compromise, isolate the host, rotate tokens/API keys, invalidate sessions and inspect audit events before restart.

