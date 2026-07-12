"""Milestone 2 safety changes.

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-12 22:55:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Add columns to orders table
    op.add_column("orders", sa.Column("approval_token", sa.String(length=32), nullable=True))
    op.add_column(
        "orders",
        sa.Column("approval_token_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(op.f("ix_orders_approval_token"), "orders", ["approval_token"], unique=False)

    # Create job_executions table
    op.create_table(
        "job_executions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("job_name", sa.String(length=100), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_job_executions_job_name"),
        "job_executions",
        ["job_name"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_job_executions_job_name"), table_name="job_executions")
    op.drop_table("job_executions")
    op.drop_index(op.f("ix_orders_approval_token"), table_name="orders")
    op.drop_column("orders", "approval_token_expires_at")
    op.drop_column("orders", "approval_token")
