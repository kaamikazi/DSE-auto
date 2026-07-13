# Durable Event Bus

The internal bus uses `outbox_events` plus `event_deliveries`. Business code stages events with a unique idempotency key. Dispatch is at-least-once. A consumer effect is recorded under a unique `(event_id, consumer)` and effect key so a correctly implemented transactional consumer applies it once.

Supported versioned events are `quote_received`, `data_activated`, `signal_generated`, `risk_rejected`, `proposal_created`, `proposal_approved`, `order_submitted`, `partial_fill`, `fill_completed`, `reconciliation_completed`, `incident_opened`, `emergency_stop`, `campaign_session_started`, and `campaign_session_completed`.

Every record carries schema version, correlation ID, optional causation ID, aggregate identity, retry/lease state, and optional audit-event linkage. Some business flows commit the outbox and canonical audit in the same database transaction; flows where canonical audit durability commits independently link the audit ID immediately afterward. Consumers must tolerate a temporarily null audit link and must never infer approval from event delivery alone.

Replay resets delivery state but does not remove the consumer effect ledger. Dead-letter replay is an authenticated operator action and remains auditable. This design does not claim true exactly-once delivery across Redis, PostgreSQL, notifications, files, or external systems.
