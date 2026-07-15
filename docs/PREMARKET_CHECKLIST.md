# Pre-Market Checklist

Run `scripts\m10-operator.ps1 premarket-check <campaign-id> --market-date YYYY-MM-DD --acknowledgement "..."`.

The gate requires a valid canonical audit chain, healthy PostgreSQL, healthy Redis, a fresh backup, healthy scheduler and worker heartbeat, healthy emergency stop, no unresolved critical incident, active campaign, configured trading calendar, active rule and fee profile, eligible data source, sufficient timestamp trust, healthy paper-account reconciliation, assigned reviewer, governance approval, explicit operator acknowledgement, and permanent paper-only safety.

Every check returns evidence and a boolean. One mandatory failure means `ready=false`; `session-start` fails closed and records the block. Do not override a failing real-market result.
