# Authoritative Evidence Registry

The registry stores DSE rules, calendars, market mechanics, fees, taxes, risk justification, strategy reviews, and market-data provenance as individually reviewable records. Every record carries source metadata, dates, identities, independence, confidence, status, claims, affected fields, optional immutable file hash, notes, and audit-event linkage.

Allowed states are `submitted`, `under_review`, `partially_verified`, `verified`, `conflicting`, `rejected`, `expired`, and `superseded`. Submission never means verification. Conflicting, expired, rejected, or missing evidence fails closed.

No registry operation activates configuration, campaigns, sessions, strategies, or orders.
