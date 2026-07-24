"""Add evidence collection and human review workspace.

Revision ID: 0011
Revises: 0010
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _id() -> sa.Column[str]:
    return sa.Column("id", sa.String(36), primary_key=True)


def upgrade() -> None:
    op.create_table(
        "evidence_collection_cases",
        _id(),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("evidence_category", sa.String(64), nullable=False),
        sa.Column("requested_documents", sa.JSON(), nullable=False),
        sa.Column("received_evidence_ids", sa.JSON(), nullable=False),
        sa.Column("missing_documents", sa.JSON(), nullable=False),
        sa.Column("responsible_collector", sa.String(100), nullable=False),
        sa.Column("reviewer", sa.String(100)),
        sa.Column("due_date", sa.Date()),
        sa.Column("review_date", sa.Date()),
        sa.Column("notes", sa.Text(), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("audit_event_ids", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    for column in ("evidence_category", "due_date", "review_date", "state"):
        op.create_index(
            f"ix_evidence_collection_cases_{column}", "evidence_collection_cases", [column]
        )
    op.create_table(
        "evidence_source_profiles",
        _id(),
        sa.Column("name", sa.String(255), nullable=False, unique=True),
        sa.Column("source_class", sa.String(64), nullable=False),
        sa.Column("hierarchy_rank", sa.Integer(), nullable=False),
        sa.Column("authority_scope", sa.JSON(), nullable=False),
        sa.Column("applicable_from", sa.Date()),
        sa.Column("applicable_to", sa.Date()),
        sa.Column("account_applicability", sa.JSON(), nullable=False),
        sa.Column("authenticity_review", sa.String(32), nullable=False),
        sa.Column("confidence", sa.String(32), nullable=False),
        sa.Column("conflicts", sa.JSON(), nullable=False),
        sa.Column("auto_verified", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    for column in ("name", "source_class"):
        op.create_index(
            f"ix_evidence_source_profiles_{column}", "evidence_source_profiles", [column]
        )
    op.create_table(
        "extracted_claims",
        _id(),
        sa.Column(
            "evidence_id", sa.String(36), sa.ForeignKey("authoritative_evidence.id"), nullable=False
        ),
        sa.Column("case_id", sa.String(36), sa.ForeignKey("evidence_collection_cases.id")),
        sa.Column("source_profile_id", sa.String(36), sa.ForeignKey("evidence_source_profiles.id")),
        sa.Column("claim_type", sa.String(100), nullable=False),
        sa.Column("source_location", sa.String(255), nullable=False),
        sa.Column("original_value", sa.Text(), nullable=False),
        sa.Column("normalized_interpretation", sa.JSON(), nullable=False),
        sa.Column("confidence", sa.String(32), nullable=False),
        sa.Column("extraction_method", sa.String(64), nullable=False),
        sa.Column("reviewer_status", sa.String(32), nullable=False),
        sa.Column("reviewer", sa.String(100)),
        sa.Column("reviewer_notes", sa.Text(), nullable=False),
        sa.Column("effective_date", sa.Date()),
        sa.Column("supporting_evidence_ids", sa.JSON(), nullable=False),
        sa.Column("conflict_reasons", sa.JSON(), nullable=False),
        sa.Column("audit_event_ids", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(timezone=True)),
    )
    for column in ("evidence_id", "case_id", "source_profile_id", "claim_type", "reviewer_status"):
        op.create_index(f"ix_extracted_claims_{column}", "extracted_claims", [column])
    op.create_table(
        "portfolio_statement_drafts",
        _id(),
        sa.Column(
            "evidence_id", sa.String(36), sa.ForeignKey("authoritative_evidence.id"), nullable=False
        ),
        sa.Column("broker_label", sa.String(100), nullable=False),
        sa.Column("account_label", sa.String(100), nullable=False),
        sa.Column("statement_date", sa.Date(), nullable=False),
        sa.Column("statement_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("parsed_data", sa.JSON(), nullable=False),
        sa.Column("reconciliation_summary", sa.JSON(), nullable=False),
        sa.Column("discrepancies", sa.JSON(), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("audit_event_ids", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reversed_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("account_label", "statement_date", "statement_hash"),
    )
    for column in ("evidence_id", "account_label", "statement_date", "statement_hash", "state"):
        op.create_index(
            f"ix_portfolio_statement_drafts_{column}", "portfolio_statement_drafts", [column]
        )
    op.create_table(
        "approval_pack_records",
        _id(),
        sa.Column("scope", sa.String(64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("pack_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("output_path", sa.Text(), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("audit_event_id", sa.String(36)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    for column in ("scope", "pack_hash", "state"):
        op.create_index(f"ix_approval_pack_records_{column}", "approval_pack_records", [column])


def downgrade() -> None:
    for table in (
        "approval_pack_records",
        "portfolio_statement_drafts",
        "extracted_claims",
        "evidence_source_profiles",
        "evidence_collection_cases",
    ):
        op.drop_table(table)
