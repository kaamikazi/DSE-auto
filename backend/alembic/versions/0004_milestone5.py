"""Milestone 5 canonical audit chain and timestamp provenance.

Revision ID: 0004
Revises: 0003
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("audit_events", sa.Column("chain_id", sa.String(36), nullable=True))
    op.add_column("audit_events", sa.Column("sequence", sa.Integer(), nullable=True))
    op.create_index("ix_audit_events_chain_id", "audit_events", ["chain_id"])
    op.create_index(
        "uq_audit_chain_sequence", "audit_events", ["chain_id", "sequence"], unique=True
    )
    op.create_table(
        "audit_chains",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("genesis_reason", sa.Text(), nullable=False),
        sa.Column("operator_acknowledgement", sa.Text(), nullable=False),
        sa.Column("legacy_archive_path", sa.Text(), nullable=False),
        sa.Column("legacy_archive_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_audit_chains_status", "audit_chains", ["status"])


def downgrade() -> None:
    op.drop_index("ix_audit_chains_status", table_name="audit_chains")
    op.drop_table("audit_chains")
    op.drop_index("uq_audit_chain_sequence", table_name="audit_events")
    op.drop_index("ix_audit_events_chain_id", table_name="audit_events")
    op.drop_column("audit_events", "sequence")
    op.drop_column("audit_events", "chain_id")
