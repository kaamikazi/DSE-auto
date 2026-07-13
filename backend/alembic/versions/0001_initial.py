"""Initial safety-critical schema."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "market_bars",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("open", sa.Numeric(18, 4), nullable=False),
        sa.Column("high", sa.Numeric(18, 4), nullable=False),
        sa.Column("low", sa.Numeric(18, 4), nullable=False),
        sa.Column("close", sa.Numeric(18, 4), nullable=False),
        sa.Column("volume", sa.Integer()),
        sa.Column("trade_count", sa.Integer()),
        sa.Column("turnover", sa.Numeric(24, 4)),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("quality_status", sa.String(32), nullable=False),
        sa.UniqueConstraint("symbol", "timestamp", "source"),
    )
    op.create_index("ix_market_bars_timestamp", "market_bars", ["timestamp"])
    op.create_index("ix_market_bars_symbol", "market_bars", ["symbol"])
    op.create_table(
        "transactions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("transaction_type", sa.String(32), nullable=False),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("quantity", sa.Numeric(18, 4), nullable=False),
        sa.Column("price", sa.Numeric(18, 4), nullable=False),
        sa.Column("fees", sa.Numeric(18, 4), nullable=False),
        sa.Column("taxes", sa.Numeric(18, 4), nullable=False),
        sa.Column("broker", sa.String(100)),
        sa.Column("account_label", sa.String(100)),
        sa.Column("notes", sa.Text()),
        sa.Column("source_record", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_transactions_occurred_at", "transactions", ["occurred_at"])
    op.create_index("ix_transactions_symbol", "transactions", ["symbol"])
    op.create_table(
        "risk_state",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("reason", sa.Text()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "orders",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("side", sa.String(8), nullable=False),
        sa.Column("order_type", sa.String(32), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("limit_price", sa.Numeric(18, 4)),
        sa.Column("stop_price", sa.Numeric(18, 4)),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("filled_quantity", sa.Integer(), nullable=False),
        sa.Column("average_fill_price", sa.Numeric(18, 4)),
        sa.Column("strategy_id", sa.String(100)),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("idempotency_key"),
    )
    op.create_index("ix_orders_idempotency_key", "orders", ["idempotency_key"], unique=True)
    op.create_index("ix_orders_symbol", "orders", ["symbol"])
    op.create_table(
        "signals",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("strategy_id", sa.String(100), nullable=False),
        sa.Column("strategy_version", sa.String(32), nullable=False),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("signal_type", sa.String(32), nullable=False),
        sa.Column("strength_score", sa.Float(), nullable=False),
        sa.Column("entry_price", sa.Numeric(18, 4)),
        sa.Column("stop_price", sa.Numeric(18, 4)),
        sa.Column("target_price", sa.Numeric(18, 4)),
        sa.Column("quantity_suggestion", sa.Integer()),
        sa.Column("reason_codes", sa.JSON(), nullable=False),
        sa.Column("explanation", sa.Text(), nullable=False),
        sa.Column("source_data_snapshot", sa.JSON(), nullable=False),
        sa.Column("data_quality_status", sa.String(32), nullable=False),
        sa.Column("risk_preview", sa.JSON(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_signals_symbol", "signals", ["symbol"])
    op.create_table(
        "audit_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actor", sa.String(100), nullable=False),
        sa.Column("event_type", sa.String(100), nullable=False),
        sa.Column("entity_type", sa.String(100), nullable=False),
        sa.Column("entity_id", sa.String(100)),
        sa.Column("previous_state", sa.JSON()),
        sa.Column("new_state", sa.JSON()),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.Column("previous_hash", sa.String(64), nullable=False),
        sa.Column("integrity_hash", sa.String(64), nullable=False, unique=True),
    )
    op.create_index("ix_audit_events_timestamp", "audit_events", ["timestamp"])
    op.create_index("ix_audit_events_event_type", "audit_events", ["event_type"])
    op.create_table(
        "paper_accounts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("cash", sa.Numeric(24, 4), nullable=False),
        sa.Column("starting_cash", sa.Numeric(24, 4), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("as_of", sa.Date(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("paper_accounts")
    op.drop_index("ix_audit_events_event_type", table_name="audit_events")
    op.drop_index("ix_audit_events_timestamp", table_name="audit_events")
    op.drop_table("audit_events")
    op.drop_index("ix_signals_symbol", table_name="signals")
    op.drop_table("signals")
    op.drop_index("ix_orders_symbol", table_name="orders")
    op.drop_index("ix_orders_idempotency_key", table_name="orders")
    op.drop_table("orders")
    op.drop_table("risk_state")
    op.drop_index("ix_transactions_symbol", table_name="transactions")
    op.drop_index("ix_transactions_occurred_at", table_name="transactions")
    op.drop_table("transactions")
    op.drop_index("ix_market_bars_symbol", table_name="market_bars")
    op.drop_index("ix_market_bars_timestamp", table_name="market_bars")
    op.drop_table("market_bars")
