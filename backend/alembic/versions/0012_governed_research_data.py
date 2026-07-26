"""Add governed research data, validation, actions, and universe persistence.

Revision ID: 0012
Revises: 0011
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _id() -> sa.Column[str]:
    return sa.Column("id", sa.String(36), primary_key=True)


def upgrade() -> None:
    op.create_table(
        "governed_datasets",
        _id(),
        sa.Column("source_category", sa.String(64), nullable=False),
        sa.Column("source_name", sa.String(255), nullable=False),
        sa.Column("source_reference", sa.Text(), nullable=False),
        sa.Column("publisher", sa.String(255), nullable=False),
        sa.Column("publication_date", sa.Date()),
        sa.Column("stated_date_coverage", sa.Text(), nullable=False),
        sa.Column("stated_symbol_coverage", sa.JSON(), nullable=False),
        sa.Column("license_note", sa.Text(), nullable=False),
        sa.Column("adjustment_status", sa.String(32), nullable=False),
        sa.Column("raw_sha256", sa.String(64), nullable=False, unique=True),
        sa.Column("file_size", sa.Integer(), nullable=False),
        sa.Column("mime_type", sa.String(128), nullable=False),
        sa.Column("raw_file_path", sa.Text(), nullable=False),
        sa.Column("imported_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("operator", sa.String(100), nullable=False),
        sa.Column("timestamp_trust", sa.String(32), nullable=False),
        sa.Column("source_trust", sa.String(32), nullable=False),
        sa.Column("review_status", sa.String(32), nullable=False),
        sa.Column("notes", sa.Text(), nullable=False),
        sa.Column("audit_event_ids", sa.JSON(), nullable=False),
    )
    for column in ("source_category", "raw_sha256", "review_status"):
        op.create_index(f"ix_governed_datasets_{column}", "governed_datasets", [column])

    op.create_table(
        "dataset_import_runs",
        _id(),
        sa.Column(
            "dataset_id", sa.String(36), sa.ForeignKey("governed_datasets.id"), nullable=False
        ),
        sa.Column("batch_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("column_mapping", sa.JSON(), nullable=False),
        sa.Column("inferred_schema", sa.JSON(), nullable=False),
        sa.Column("preview", sa.JSON(), nullable=False),
        sa.Column("normalized_file_path", sa.Text()),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("row_count", sa.Integer(), nullable=False),
        sa.Column("errors", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("activated_at", sa.DateTime(timezone=True)),
        sa.Column("rolled_back_at", sa.DateTime(timezone=True)),
    )
    for column in ("dataset_id", "batch_hash", "state"):
        op.create_index(f"ix_dataset_import_runs_{column}", "dataset_import_runs", [column])

    op.create_table(
        "normalized_daily_bars",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "dataset_id", sa.String(36), sa.ForeignKey("governed_datasets.id"), nullable=False
        ),
        sa.Column(
            "import_run_id", sa.String(36), sa.ForeignKey("dataset_import_runs.id"), nullable=False
        ),
        sa.Column("batch_hash", sa.String(64), nullable=False),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("trading_date", sa.Date(), nullable=False),
        sa.Column("open", sa.Numeric(18, 4), nullable=False),
        sa.Column("high", sa.Numeric(18, 4), nullable=False),
        sa.Column("low", sa.Numeric(18, 4), nullable=False),
        sa.Column("close", sa.Numeric(18, 4), nullable=False),
        sa.Column("adjusted_close", sa.Numeric(18, 4)),
        sa.Column("volume", sa.Numeric(24, 4), nullable=False),
        sa.Column("value", sa.Numeric(24, 4)),
        sa.Column("number_of_trades", sa.Integer()),
        sa.Column("previous_close", sa.Numeric(18, 4)),
        sa.Column("source", sa.String(255), nullable=False),
        sa.Column("source_row_id", sa.String(128), nullable=False),
        sa.Column("adjusted", sa.Boolean(), nullable=False),
        sa.Column("timestamp_trust", sa.String(32), nullable=False),
        sa.Column("timestamp_provenance", sa.JSON(), nullable=False),
        sa.Column("active_for_research", sa.Boolean(), nullable=False),
        sa.UniqueConstraint("dataset_id", "symbol", "trading_date", "source_row_id"),
    )
    for column in (
        "dataset_id",
        "import_run_id",
        "batch_hash",
        "symbol",
        "trading_date",
        "active_for_research",
    ):
        op.create_index(f"ix_normalized_daily_bars_{column}", "normalized_daily_bars", [column])

    op.create_table(
        "cross_source_validation_runs",
        _id(),
        sa.Column(
            "primary_dataset_id",
            sa.String(36),
            sa.ForeignKey("governed_datasets.id"),
            nullable=False,
        ),
        sa.Column(
            "secondary_dataset_id",
            sa.String(36),
            sa.ForeignKey("governed_datasets.id"),
            nullable=False,
        ),
        sa.Column("report", sa.JSON(), nullable=False),
        sa.Column("report_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("output_paths", sa.JSON(), nullable=False),
        sa.Column("review_status", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "corporate_action_records",
        _id(),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("announcement_date", sa.Date()),
        sa.Column("ex_date", sa.Date()),
        sa.Column("record_date", sa.Date()),
        sa.Column("effective_date", sa.Date()),
        sa.Column("ratio_or_amount", sa.String(64)),
        sa.Column("source_evidence_ids", sa.JSON(), nullable=False),
        sa.Column("verification_status", sa.String(32), nullable=False),
        sa.Column("adjustment_factor", sa.Numeric(18, 8)),
        sa.Column("affected_dataset_ids", sa.JSON(), nullable=False),
        sa.Column("review_decision", sa.String(32), nullable=False),
        sa.Column("inferred", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_corporate_action_records_symbol", "corporate_action_records", ["symbol"])
    op.create_index(
        "ix_corporate_action_records_event_type", "corporate_action_records", ["event_type"]
    )
    op.create_table(
        "research_universe_versions",
        _id(),
        sa.Column("name", sa.String(150), nullable=False, unique=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("methodology", sa.JSON(), nullable=False),
        sa.Column("version_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_research_universe_versions_status", "research_universe_versions", ["status"]
    )
    op.create_table(
        "universe_membership_periods",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "universe_id",
            sa.String(36),
            sa.ForeignKey("research_universe_versions.id"),
            nullable=False,
        ),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("eligible_from", sa.Date(), nullable=False),
        sa.Column("eligible_to", sa.Date()),
        sa.Column("listing_date", sa.Date()),
        sa.Column("delisting_date", sa.Date()),
        sa.Column("suspension_periods", sa.JSON(), nullable=False),
        sa.Column("category_history", sa.JSON(), nullable=False),
        sa.Column("symbol_changes", sa.JSON(), nullable=False),
        sa.Column("missing_data_periods", sa.JSON(), nullable=False),
        sa.Column("liquidity_history", sa.JSON(), nullable=False),
        sa.Column("sector", sa.String(100)),
        sa.Column("market_cap_metadata", sa.JSON(), nullable=False),
    )
    op.create_index(
        "ix_universe_membership_periods_universe_id", "universe_membership_periods", ["universe_id"]
    )
    op.create_index(
        "ix_universe_membership_periods_symbol", "universe_membership_periods", ["symbol"]
    )


def downgrade() -> None:
    for table in (
        "universe_membership_periods",
        "research_universe_versions",
        "corporate_action_records",
        "cross_source_validation_runs",
        "normalized_daily_bars",
        "dataset_import_runs",
        "governed_datasets",
    ):
        op.drop_table(table)
