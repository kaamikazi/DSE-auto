"""Add production-like paper infrastructure records.

Revision ID: 0008
Revises: 0007
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _timestamps() -> list[sa.Column[object]]:
    return [sa.Column("created_at", sa.DateTime(timezone=True), nullable=False)]


def upgrade() -> None:
    op.create_table(
        "task_records",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("task_name", sa.String(100), nullable=False),
        sa.Column("queue", sa.String(100), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False, unique=True),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_owner", sa.String(100)),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.Text()),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("correlation_id", sa.String(100)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    for column in (
        "task_name",
        "queue",
        "idempotency_key",
        "state",
        "available_at",
        "lease_owner",
        "lease_expires_at",
        "correlation_id",
    ):
        op.create_index(f"ix_task_records_{column}", "task_records", [column])

    op.create_table(
        "worker_heartbeats",
        sa.Column("worker_id", sa.String(100), primary_key=True),
        sa.Column("process_id", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("queues", sa.JSON(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("shutdown_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_worker_heartbeats_state", "worker_heartbeats", ["state"])
    op.create_index("ix_worker_heartbeats_heartbeat_at", "worker_heartbeats", ["heartbeat_at"])

    op.create_table(
        "outbox_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("event_type", sa.String(100), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("aggregate_type", sa.String(100), nullable=False),
        sa.Column("aggregate_id", sa.String(100)),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False, unique=True),
        sa.Column("correlation_id", sa.String(100), nullable=False),
        sa.Column("causation_id", sa.String(100)),
        sa.Column("audit_event_id", sa.String(36)),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_owner", sa.String(100)),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("delivered_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    for column in (
        "event_type",
        "aggregate_id",
        "idempotency_key",
        "correlation_id",
        "causation_id",
        "audit_event_id",
        "state",
        "available_at",
    ):
        op.create_index(f"ix_outbox_events_{column}", "outbox_events", [column])

    op.create_table(
        "event_deliveries",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("event_id", sa.String(36), sa.ForeignKey("outbox_events.id"), nullable=False),
        sa.Column("consumer", sa.String(100), nullable=False),
        sa.Column("effect_key", sa.String(255), nullable=False),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("event_id", "consumer"),
        sa.UniqueConstraint("consumer", "effect_key"),
    )
    op.create_index("ix_event_deliveries_event_id", "event_deliveries", ["event_id"])

    op.create_table(
        "data_quality_observations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("campaign_id", sa.String(36)),
        sa.Column("market_date", sa.Date(), nullable=False),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("provider", sa.String(100), nullable=False),
        sa.Column("timestamp_trust", sa.String(32), nullable=False),
        sa.Column("metrics", sa.JSON(), nullable=False),
        sa.Column("passed", sa.Boolean(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
    )
    for column in ("campaign_id", "market_date", "symbol", "provider", "passed"):
        op.create_index(
            f"ix_data_quality_observations_{column}", "data_quality_observations", [column]
        )

    op.create_table(
        "data_quality_reports",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("scope", sa.String(32), nullable=False),
        sa.Column("campaign_id", sa.String(36)),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("metrics", sa.JSON(), nullable=False),
        sa.Column("json_path", sa.Text(), nullable=False),
        sa.Column("csv_path", sa.Text(), nullable=False),
        sa.Column("chart_path", sa.Text(), nullable=False),
        sa.Column("integrity_hash", sa.String(64), nullable=False),
        sa.Column("passed", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("scope", "campaign_id", "start_date", "end_date"),
    )
    for column in ("scope", "campaign_id", "integrity_hash"):
        op.create_index(f"ix_data_quality_reports_{column}", "data_quality_reports", [column])

    op.create_table(
        "evidence_reviews",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "campaign_day_id",
            sa.Integer(),
            sa.ForeignKey("campaign_days.id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("campaign_id", sa.String(36), nullable=False),
        sa.Column("session_id", sa.String(36)),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("reviewer", sa.String(100)),
        sa.Column("reviewer_role", sa.String(32)),
        sa.Column("reviewed_at", sa.DateTime(timezone=True)),
        sa.Column("evidence_pack_hash", sa.String(64), nullable=False),
        sa.Column("data_quality_verdict", sa.String(32)),
        sa.Column("strategy_behavior_verdict", sa.String(32)),
        sa.Column("risk_engine_verdict", sa.String(32)),
        sa.Column("execution_model_verdict", sa.String(32)),
        sa.Column("incidents_reviewed", sa.JSON(), nullable=False),
        sa.Column("comments", sa.Text(), nullable=False),
        sa.Column("approval_decision", sa.String(32)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    for column in ("campaign_day_id", "campaign_id", "session_id", "state"):
        op.create_index(f"ix_evidence_reviews_{column}", "evidence_reviews", [column])

    op.create_table(
        "paper_qualifications",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("campaign_id", sa.String(36), nullable=False, unique=True),
        sa.Column("target_days", sa.Integer(), nullable=False),
        sa.Column("counts", sa.JSON(), nullable=False),
        sa.Column("qualifying", sa.Boolean(), nullable=False),
        sa.Column("failure_reasons", sa.JSON(), nullable=False),
        sa.Column("remaining_qualifying_days", sa.Integer(), nullable=False),
        sa.Column("calculated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_paper_qualifications_campaign_id", "paper_qualifications", ["campaign_id"])

    op.create_table(
        "risk_validation_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("campaign_id", sa.String(36)),
        sa.Column("report", sa.JSON(), nullable=False),
        sa.Column("integrity_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_risk_validation_runs_campaign_id", "risk_validation_runs", ["campaign_id"])
    op.create_index(
        "ix_risk_validation_runs_integrity_hash", "risk_validation_runs", ["integrity_hash"]
    )

    op.create_table(
        "disaster_recovery_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("recovery_point_seconds", sa.Float(), nullable=False),
        sa.Column("recovery_time_seconds", sa.Float(), nullable=False),
        sa.Column("checks", sa.JSON(), nullable=False),
        sa.Column("evidence_path", sa.Text(), nullable=False),
        sa.Column("integrity_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_disaster_recovery_runs_status", "disaster_recovery_runs", ["status"])
    op.create_index(
        "ix_disaster_recovery_runs_integrity_hash", "disaster_recovery_runs", ["integrity_hash"]
    )

    op.create_table(
        "database_migration_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("source_url_redacted", sa.Text(), nullable=False),
        sa.Column("destination_url_redacted", sa.Text(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("record_counts", sa.JSON(), nullable=False),
        sa.Column("table_hashes", sa.JSON(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_database_migration_runs_status", "database_migration_runs", ["status"])

    op.create_table(
        "auth_sessions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("role", sa.String(32), nullable=False),
        sa.Column("actor", sa.String(100), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    for column in ("token_hash", "role", "expires_at"):
        op.create_index(f"ix_auth_sessions_{column}", "auth_sessions", [column])

    op.create_table(
        "login_attempts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("role", sa.String(32), nullable=False),
        sa.Column("succeeded", sa.Boolean(), nullable=False),
        sa.Column("attempted_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_login_attempts_fingerprint", "login_attempts", ["fingerprint"])
    op.create_index("ix_login_attempts_attempted_at", "login_attempts", ["attempted_at"])


def downgrade() -> None:
    for table in (
        "login_attempts",
        "auth_sessions",
        "database_migration_runs",
        "disaster_recovery_runs",
        "risk_validation_runs",
        "paper_qualifications",
        "evidence_reviews",
        "data_quality_reports",
        "data_quality_observations",
        "event_deliveries",
        "outbox_events",
        "worker_heartbeats",
        "task_records",
    ):
        op.drop_table(table)
