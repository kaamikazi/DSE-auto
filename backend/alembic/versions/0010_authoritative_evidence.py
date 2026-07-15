"""Add authoritative evidence and pre-campaign governance records.

Revision ID: 0010
Revises: 0009
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _id() -> sa.Column[str]:
    return sa.Column("id", sa.String(36), primary_key=True)


def upgrade() -> None:
    op.create_table(
        "authoritative_evidence",
        _id(),
        sa.Column("category", sa.String(64), nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("source_organization", sa.String(255), nullable=False),
        sa.Column("source_type", sa.String(64), nullable=False),
        sa.Column("source_reference", sa.Text(), nullable=False),
        sa.Column("document_date", sa.Date()),
        sa.Column("effective_date", sa.Date()),
        sa.Column("review_date", sa.Date()),
        sa.Column("collected_by", sa.String(100), nullable=False),
        sa.Column("reviewed_by", sa.String(100)),
        sa.Column("reviewer_independence", sa.String(32), nullable=False),
        sa.Column("confidence", sa.String(32), nullable=False),
        sa.Column("verification_status", sa.String(32), nullable=False),
        sa.Column("extracted_claim", sa.Text(), nullable=False),
        sa.Column("affected_fields", sa.JSON(), nullable=False),
        sa.Column("file_hash", sa.String(64), unique=True),
        sa.Column("raw_file_path", sa.Text()),
        sa.Column("original_filename", sa.String(255)),
        sa.Column("media_type", sa.String(100)),
        sa.Column("file_size", sa.Integer()),
        sa.Column("source_description", sa.Text(), nullable=False),
        sa.Column("operator_attestation", sa.Text(), nullable=False),
        sa.Column("extraction", sa.JSON(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=False),
        sa.Column("audit_event_ids", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    for column in ("category", "review_date", "verification_status", "file_hash"):
        op.create_index(f"ix_authoritative_evidence_{column}", "authoritative_evidence", [column])
    op.create_table(
        "governance_item_approvals",
        _id(),
        sa.Column("approval_type", sa.String(32), nullable=False),
        sa.Column("draft_version", sa.String(64), nullable=False),
        sa.Column("item_key", sa.String(100), nullable=False),
        sa.Column("current_draft", sa.JSON(), nullable=False),
        sa.Column("proposed_value", sa.JSON(), nullable=False),
        sa.Column("evidence_ids", sa.JSON(), nullable=False),
        sa.Column("conflicts", sa.JSON(), nullable=False),
        sa.Column("missing_evidence", sa.JSON(), nullable=False),
        sa.Column("verification_status", sa.String(32), nullable=False),
        sa.Column("approval_status", sa.String(32), nullable=False),
        sa.Column("effective_date", sa.Date()),
        sa.Column("operator_identity", sa.String(100)),
        sa.Column("reviewer_identity", sa.String(100)),
        sa.Column("reviewer_independence", sa.String(32), nullable=False),
        sa.Column("conservative_fallback", sa.JSON(), nullable=False),
        sa.Column("decision_hash", sa.String(64), unique=True),
        sa.Column("audit_event_id", sa.String(36)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("approval_type", "draft_version", "item_key"),
    )
    for column in (
        "approval_type",
        "draft_version",
        "item_key",
        "verification_status",
        "approval_status",
    ):
        op.create_index(
            f"ix_governance_item_approvals_{column}", "governance_item_approvals", [column]
        )
    op.create_table(
        "reviewer_invitations",
        _id(),
        sa.Column("reviewer_identity", sa.String(100), nullable=False),
        sa.Column("role", sa.String(64), nullable=False),
        sa.Column("access_scope", sa.JSON(), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("conflict_declaration", sa.Text(), nullable=False),
        sa.Column("independence", sa.String(32), nullable=False),
        sa.Column("invited_by", sa.String(100), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("audit_event_id", sa.String(36)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    for column in ("reviewer_identity", "state", "expires_at"):
        op.create_index(f"ix_reviewer_invitations_{column}", "reviewer_invitations", [column])
    op.create_table(
        "review_assignments",
        _id(),
        sa.Column(
            "invitation_id", sa.String(36), sa.ForeignKey("reviewer_invitations.id"), nullable=False
        ),
        sa.Column("subject_type", sa.String(64), nullable=False),
        sa.Column("subject_id", sa.String(100), nullable=False),
        sa.Column("evidence_hash", sa.String(64), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("decision", sa.String(32)),
        sa.Column("comments", sa.Text(), nullable=False),
        sa.Column("independence", sa.String(32), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(timezone=True)),
        sa.Column("audit_event_id", sa.String(36)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    for column in ("invitation_id", "subject_type", "subject_id", "state", "expires_at"):
        op.create_index(f"ix_review_assignments_{column}", "review_assignments", [column])
    op.create_table(
        "research_datasets",
        _id(),
        sa.Column("name", sa.String(150), nullable=False, unique=True),
        sa.Column("symbols", sa.JSON(), nullable=False),
        sa.Column("data_types", sa.JSON(), nullable=False),
        sa.Column("source_evidence_ids", sa.JSON(), nullable=False),
        sa.Column("source_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("dataset_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("timestamp_trust", sa.String(32), nullable=False),
        sa.Column("raw_file_path", sa.Text(), nullable=False),
        sa.Column("normalized_file_path", sa.Text(), nullable=False),
        sa.Column("quality_report", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("approved_by", sa.String(100)),
        sa.Column("approved_at", sa.DateTime(timezone=True)),
        sa.Column("audit_event_ids", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    for column in ("name", "source_hash", "dataset_hash", "status"):
        op.create_index(f"ix_research_datasets_{column}", "research_datasets", [column])
    op.create_table(
        "risk_calibration_runs",
        _id(),
        sa.Column("strategy_registration_id", sa.String(36), nullable=False),
        sa.Column("report", sa.JSON(), nullable=False),
        sa.Column("integrity_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    for column in ("strategy_registration_id", "integrity_hash", "status"):
        op.create_index(f"ix_risk_calibration_runs_{column}", "risk_calibration_runs", [column])
    op.create_table(
        "strategy_readiness_reports",
        _id(),
        sa.Column("strategy_registration_id", sa.String(36), nullable=False),
        sa.Column("status", sa.String(64), nullable=False),
        sa.Column("checks", sa.JSON(), nullable=False),
        sa.Column("missing_items", sa.JSON(), nullable=False),
        sa.Column("report_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    for column in ("strategy_registration_id", "status", "report_hash"):
        op.create_index(
            f"ix_strategy_readiness_reports_{column}", "strategy_readiness_reports", [column]
        )


def downgrade() -> None:
    for table in (
        "strategy_readiness_reports",
        "risk_calibration_runs",
        "research_datasets",
        "review_assignments",
        "reviewer_invitations",
        "governance_item_approvals",
        "authoritative_evidence",
    ):
        op.drop_table(table)
