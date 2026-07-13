"""Add provider certification and evidence-class isolation.

Revision ID: 0009
Revises: 0008
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "validation_campaigns",
        sa.Column("evidence_class", sa.String(32), nullable=False, server_default="synthetic"),
    )
    op.add_column("validation_campaigns", sa.Column("provider_certification_id", sa.String(36)))
    op.add_column(
        "validation_campaigns",
        sa.Column("daily_reviewer_assignments", sa.JSON(), nullable=False, server_default="{}"),
    )
    op.create_index(
        "ix_validation_campaigns_evidence_class", "validation_campaigns", ["evidence_class"]
    )
    op.create_index(
        "ix_validation_campaigns_provider_certification_id",
        "validation_campaigns",
        ["provider_certification_id"],
    )
    op.add_column(
        "campaign_days",
        sa.Column("evidence_class", sa.String(32), nullable=False, server_default="synthetic"),
    )
    op.create_index("ix_campaign_days_evidence_class", "campaign_days", ["evidence_class"])
    op.create_table(
        "provider_certifications",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("provider_id", sa.String(100), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("report", sa.JSON(), nullable=False),
        sa.Column("integrity_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("certified_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_provider_certifications_provider_id", "provider_certifications", ["provider_id"]
    )
    op.create_index("ix_provider_certifications_status", "provider_certifications", ["status"])
    op.create_index(
        "ix_provider_certifications_integrity_hash",
        "provider_certifications",
        ["integrity_hash"],
    )


def downgrade() -> None:
    op.drop_table("provider_certifications")
    op.drop_index("ix_campaign_days_evidence_class", table_name="campaign_days")
    op.drop_column("campaign_days", "evidence_class")
    op.drop_index(
        "ix_validation_campaigns_provider_certification_id", table_name="validation_campaigns"
    )
    op.drop_index("ix_validation_campaigns_evidence_class", table_name="validation_campaigns")
    op.drop_column("validation_campaigns", "daily_reviewer_assignments")
    op.drop_column("validation_campaigns", "provider_certification_id")
    op.drop_column("validation_campaigns", "evidence_class")
