# Accelerated Soak Test Report

The deterministic 60-day test processes 480 scheduled events and injects three recoveries. It asserts unique order keys, non-negative cash, no short positions and complete recovery accounting. Other outage modes remain covered by backend failure tests. A failed invariant fails the test; none is ignored.

## Unresolved invariant

The restored operational database contains one historical audit-chain branch created by concurrent Milestone 2 writers: `signal.generated` and `data.quotes_refreshed` reference the same predecessor. The chain is invalid and operations must remain fail-closed in `reconciliation_required`. The records were not rewritten or discarded. Remediation requires an operator-approved archive/new-chain procedure and serialization of audit writers before continuous sessions.
