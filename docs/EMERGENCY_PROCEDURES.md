# Emergency Procedures

For scheduler or provider incidents, stop paper execution, preserve a database backup, resolve stale/failed jobs and disagreement, then restart exactly one scheduler-enabled process. Startup recovery must reconcile cash, holdings, open orders and pending proposals before a human resumes operations.

1. **Trigger Emergency Stop**:
   - Operator Dashboard: Click the **EMERGENCY STOP** button.
   - Telegram Bot: Send the `/emergency_stop` command.
   - API: Submit a request to `POST /api/v1/risk/emergency-stop` (requires `X-API-Key`).
   - Host Isolation: Stop the FastAPI backend process.
2. **System Suspension (Pause)**:
   - Telegram Bot: Send the `/pause` command.
   - Dashboard: Click the **PAUSE SYSTEM** button.
   - API: Submit a request to `POST /api/v1/risk/pause`.
3. **Log & Database Preservation**: Preserve SQLite databases and the chained audit logs. Do not edit, truncate, or rewrite logs.
4. **Reconciliation Checks**:
   - Compare data sources to identify staleness or feed disagreement.
   - If the system restarts with pending or open orders, the startup recovery checks will fail-closed, setting the risk state to `reconciliation_required`.
5. **System Recovery & Resumption**:
   - Reconcile derived cash balance and positions with recorded transaction events.
   - Send `/resume` via Telegram or click **RECONCILE & RESUME** on the dashboard.
   - Resumption will automatically verify the audit trail chain hash signatures. If discrepancies are found, the system remains in `reconciliation_required` state.
