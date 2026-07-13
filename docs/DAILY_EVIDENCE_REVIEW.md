# Daily Evidence Review

A completed campaign day is automatically queued as `pending_review`. It does not count toward qualification until a human records a decision.

States are `pending_review`, `reviewed`, `accepted`, `concerns_found`, `rejected`, and `requires_rerun`. Final decisions are immutable; remediation creates a new/rerun day rather than rewriting evidence.

The review captures reviewer and role, timestamp, campaign/session, evidence-pack hash, data-quality verdict, strategy-behavior verdict, risk-engine verdict, execution-model verdict, reviewed incidents, comments, and approval decision. The canonical audit chain records the state change.

Reviewer credentials can read protected operational evidence and submit evidence attestations. They cannot pause/resume, replay outbox events, run recovery, change risk state, or perform other operator mutations. Operator credentials may perform both roles, but independent review is still required by policy.
