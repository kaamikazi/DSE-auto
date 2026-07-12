"""Milestone 3 paper validation entities.

Revision ID: 0003
Revises: 0002
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "paper_sessions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("starting_cash", sa.Numeric(24, 4), nullable=False),
        sa.Column("approved_universe", sa.JSON(), nullable=False),
        sa.Column("strategies", sa.JSON(), nullable=False),
        sa.Column("risk_profile", sa.JSON(), nullable=False),
        sa.Column("fill_model", sa.String(16), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("stopped_at", sa.DateTime(timezone=True)),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("name"),
    )
    op.create_index("ix_paper_sessions_name", "paper_sessions", ["name"])
    op.create_index("ix_paper_sessions_account_id", "paper_sessions", ["account_id"])
    op.create_index("ix_paper_sessions_state", "paper_sessions", ["state"])
    op.create_table(
        "paper_session_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("session_id", sa.String(36), nullable=False),
        sa.Column("run_type", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("reason", sa.Text()),
        sa.Column("metrics", sa.JSON(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_paper_session_runs_session_id", "paper_session_runs", ["session_id"])
    op.create_index("ix_paper_session_runs_run_type", "paper_session_runs", ["run_type"])
    op.create_table(
        "import_batches",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("source_name", sa.String(255), nullable=False),
        sa.Column("source_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("row_count", sa.Integer(), nullable=False),
        sa.Column("transaction_ids", sa.JSON(), nullable=False),
        sa.Column("errors", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reversed_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_import_batches_source_hash", "import_batches", ["source_hash"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_import_batches_source_hash", table_name="import_batches")
    op.drop_table("import_batches")
    op.drop_index("ix_paper_session_runs_run_type", table_name="paper_session_runs")
    op.drop_index("ix_paper_session_runs_session_id", table_name="paper_session_runs")
    op.drop_table("paper_session_runs")
    op.drop_index("ix_paper_sessions_state", table_name="paper_sessions")
    op.drop_index("ix_paper_sessions_account_id", table_name="paper_sessions")
    op.drop_index("ix_paper_sessions_name", table_name="paper_sessions")
    op.drop_table("paper_sessions")
