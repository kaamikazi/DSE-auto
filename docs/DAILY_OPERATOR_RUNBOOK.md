# Daily Operator Runbook

1. Confirm PAPER TRADING and LIVE TRADING DISABLED.
2. Verify audit, backup, calendar and provider diagnostics.
3. Start one named session and activate after reconciliation.
4. Monitor heartbeats, trusted timestamps, skips, fills and risk.
5. Pause on disagreement; emergency-stop on integrity uncertainty.
6. Run end-of-day once, generate evidence and back up.
7. Complete the session and review unresolved failures.

## Milestone 5 commands

- `operator.py verify-data --provider <name> --symbol GP`
- `operator.py audit-status` and `operator.py verify-audit`
- `operator.py session start|pause|resume|stop <name>`
- `operator.py readiness --provider attested_csv --symbol GP --acknowledgement "..."`
- `operator.py reconcile`
- `operator.py run-imported-session --acknowledgement "..."` (includes EOD and evidence)
- `operator.py emergency-stop`
- `backup.ps1`

The Windows menu remains `scripts\paper-operator.ps1`. All execution is paper-only.

The dashboard exposes read-only Verify Data and Verify Audit actions plus readiness status. Mutating actions use authenticated CLI/API operations; the browser must not embed an operator API secret for backup, EOD, or evidence generation.
