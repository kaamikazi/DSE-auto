"""Initial safety-critical schema."""

from collections.abc import Sequence

from alembic import op
from app.core.database import Base
from app.models import *  # noqa: F403

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    Base.metadata.create_all(bind=op.get_bind())


def downgrade() -> None:
    Base.metadata.drop_all(bind=op.get_bind())
