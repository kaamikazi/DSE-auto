"""Milestone 6 sustained campaign operations.

Revision ID: 0005
Revises: 0004
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for table in ("transactions", "orders", "paper_sessions"):
        op.add_column(table, sa.Column("campaign_id", sa.String(36), nullable=True))
        op.create_index(f"ix_{table}_campaign_id", table, ["campaign_id"])
    op.add_column(
        "import_batches",
        sa.Column("import_kind", sa.String(32), nullable=False, server_default="transactions"),
    )
    op.add_column("import_batches", sa.Column("market_date", sa.Date(), nullable=True))
    op.add_column("import_batches", sa.Column("operator_attestation", sa.Text(), nullable=True))
    op.add_column("import_batches", sa.Column("raw_file_path", sa.Text(), nullable=True))
    op.add_column(
        "import_batches", sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("import_batches", sa.Column("campaign_id", sa.String(36), nullable=True))
    op.create_index("ix_import_batches_campaign_id", "import_batches", ["campaign_id"])
    op.add_column(
        "market_bars",
        sa.Column("timestamp_provenance", sa.String(32), nullable=False, server_default="unknown"),
    )
    op.add_column("market_bars", sa.Column("import_batch_id", sa.String(36), nullable=True))
    op.add_column("market_bars", sa.Column("campaign_id", sa.String(36), nullable=True))
    op.create_index("ix_market_bars_import_batch_id", "market_bars", ["import_batch_id"])
    op.create_index("ix_market_bars_campaign_id", "market_bars", ["campaign_id"])
    op.add_column("signals", sa.Column("campaign_id", sa.String(36), nullable=True))
    op.create_index("ix_signals_campaign_id", "signals", ["campaign_id"])
    op.create_table(
        "validation_campaigns",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False, unique=True),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("planned_end_date", sa.Date(), nullable=False),
        sa.Column("approved_symbols", sa.JSON(), nullable=False),
        sa.Column("approved_strategies", sa.JSON(), nullable=False),
        sa.Column("starting_capital", sa.Numeric(24, 4), nullable=False),
        sa.Column("risk_profile", sa.JSON(), nullable=False),
        sa.Column("data_source_policy", sa.JSON(), nullable=False),
        sa.Column("timestamp_trust_requirement", sa.String(32), nullable=False),
        sa.Column("fill_model", sa.String(16), nullable=False),
        sa.Column("benchmark", sa.String(32), nullable=False),
        sa.Column("operator_notes", sa.Text(), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("active_rule_set_id", sa.String(36), nullable=False),
        sa.Column("active_fee_profile_id", sa.String(36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_validation_campaigns_name", "validation_campaigns", ["name"])
    op.create_index("ix_validation_campaigns_account_id", "validation_campaigns", ["account_id"])
    op.create_index("ix_validation_campaigns_state", "validation_campaigns", ["state"])
    op.create_table(
        "campaign_days",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("campaign_id", sa.String(36), nullable=False),
        sa.Column("market_date", sa.Date(), nullable=False),
        sa.Column("session_id", sa.String(36)),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("premarket_completed", sa.Boolean(), nullable=False),
        sa.Column("eod_completed", sa.Boolean(), nullable=False),
        sa.Column("missed_reason", sa.Text()),
        sa.Column("summary", sa.JSON(), nullable=False),
        sa.Column("evidence_path", sa.Text()),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("campaign_id", "market_date"),
    )
    op.create_index("ix_campaign_days_campaign_id", "campaign_days", ["campaign_id"])
    op.create_index("ix_campaign_days_market_date", "campaign_days", ["market_date"])
    op.create_table(
        "market_rule_sets",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("version", sa.String(32), nullable=False, unique=True),
        sa.Column("effective_date", sa.Date(), nullable=False),
        sa.Column("source_reference", sa.Text(), nullable=False),
        sa.Column("verification_status", sa.String(32), nullable=False),
        sa.Column("operator_approval", sa.Text(), nullable=False),
        sa.Column("rules", sa.JSON(), nullable=False),
        sa.Column("change_history", sa.JSON(), nullable=False),
        sa.Column("integrity_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_market_rule_sets_version", "market_rule_sets", ["version"])
    op.create_table(
        "fee_profiles",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("version", sa.String(32), nullable=False),
        sa.Column("effective_date", sa.Date(), nullable=False),
        sa.Column("broker", sa.String(100)),
        sa.Column("account_label", sa.String(100)),
        sa.Column("configuration", sa.JSON(), nullable=False),
        sa.Column("integrity_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("name", "version"),
    )
    op.create_index("ix_fee_profiles_name", "fee_profiles", ["name"])
    op.create_table(
        "strategy_registrations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("strategy_id", sa.String(100), nullable=False),
        sa.Column("version", sa.String(32), nullable=False),
        sa.Column("lifecycle_state", sa.String(32), nullable=False),
        sa.Column("code_hash", sa.String(64), nullable=False),
        sa.Column("parameters", sa.JSON(), nullable=False),
        sa.Column("data_requirements", sa.JSON(), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("minimum_sample_size", sa.Integer(), nullable=False),
        sa.Column("operator_approval", sa.Text()),
        sa.Column("suspension_reason", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("strategy_id", "version"),
    )
    op.create_index(
        "ix_strategy_registrations_strategy_id", "strategy_registrations", ["strategy_id"]
    )
    op.create_index(
        "ix_strategy_registrations_lifecycle_state", "strategy_registrations", ["lifecycle_state"]
    )
    op.create_table(
        "operational_incidents",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("campaign_id", sa.String(36)),
        sa.Column("incident_type", sa.String(64), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("severity", sa.String(16), nullable=False),
        sa.Column("owner", sa.String(100)),
        sa.Column("root_cause", sa.Text()),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("remediation", sa.Text()),
        sa.Column("linked_audit_events", sa.JSON(), nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True)),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
    )
    op.create_index(
        "ix_operational_incidents_campaign_id", "operational_incidents", ["campaign_id"]
    )
    op.create_index(
        "ix_operational_incidents_incident_type", "operational_incidents", ["incident_type"]
    )
    op.create_index("ix_operational_incidents_state", "operational_incidents", ["state"])
    op.create_table(
        "operational_metrics",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("campaign_id", sa.String(36)),
        sa.Column("metric_name", sa.String(100), nullable=False),
        sa.Column("value", sa.Float(), nullable=False),
        sa.Column("unit", sa.String(32), nullable=False),
        sa.Column("labels", sa.JSON(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_operational_metrics_campaign_id", "operational_metrics", ["campaign_id"])
    op.create_index("ix_operational_metrics_metric_name", "operational_metrics", ["metric_name"])
    op.create_index("ix_operational_metrics_recorded_at", "operational_metrics", ["recorded_at"])


def downgrade() -> None:
    for table in (
        "operational_metrics",
        "operational_incidents",
        "strategy_registrations",
        "fee_profiles",
        "market_rule_sets",
        "campaign_days",
        "validation_campaigns",
    ):
        op.drop_table(table)
    op.drop_index("ix_signals_campaign_id", table_name="signals")
    op.drop_column("signals", "campaign_id")
    op.drop_index("ix_market_bars_campaign_id", table_name="market_bars")
    op.drop_index("ix_market_bars_import_batch_id", table_name="market_bars")
    for column in ("campaign_id", "import_batch_id", "timestamp_provenance"):
        op.drop_column("market_bars", column)
    op.drop_index("ix_import_batches_campaign_id", table_name="import_batches")
    for column in (
        "campaign_id",
        "activated_at",
        "raw_file_path",
        "operator_attestation",
        "market_date",
        "import_kind",
    ):
        op.drop_column("import_batches", column)
    for table in ("paper_sessions", "orders", "transactions"):
        op.drop_index(f"ix_{table}_campaign_id", table_name=table)
        op.drop_column(table, "campaign_id")
