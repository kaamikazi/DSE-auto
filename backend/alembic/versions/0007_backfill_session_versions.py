"""Backfill locked campaign versions on existing daily sessions.

Revision ID: 0007
Revises: 0006
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE paper_sessions
        SET market_rule_set_id = (
            SELECT active_rule_set_id FROM validation_campaigns
            WHERE validation_campaigns.id = paper_sessions.campaign_id
        ),
        fee_profile_id = (
            SELECT active_fee_profile_id FROM validation_campaigns
            WHERE validation_campaigns.id = paper_sessions.campaign_id
        )
        WHERE campaign_id IS NOT NULL
        """
    )


def downgrade() -> None:
    # Evidence is intentionally preserved. Revision 0006 owns the columns.
    pass
