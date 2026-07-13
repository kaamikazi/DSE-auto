"""Record exact market-rule and fee versions on paper sessions.

Revision ID: 0006
Revises: 0005
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("paper_sessions", sa.Column("market_rule_set_id", sa.String(36)))
    op.add_column("paper_sessions", sa.Column("fee_profile_id", sa.String(36)))


def downgrade() -> None:
    op.drop_column("paper_sessions", "fee_profile_id")
    op.drop_column("paper_sessions", "market_rule_set_id")
