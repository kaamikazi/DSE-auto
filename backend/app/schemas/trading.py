from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

TransactionType = Literal[
    "buy",
    "sell",
    "dividend",
    "bonus",
    "split",
    "rights",
    "fee",
    "tax",
    "adjustment",
    "cash_balance",
]


class TransactionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    occurred_at: datetime
    transaction_type: TransactionType
    symbol: str
    quantity: Decimal = Field(default=Decimal("0"), ge=0)
    price: Decimal = Field(default=Decimal("0"), ge=0)
    fees: Decimal = Field(default=Decimal("0"), ge=0)
    taxes: Decimal = Field(default=Decimal("0"), ge=0)
    broker: str | None = None
    account_label: str | None = None
    notes: str | None = None

    @model_validator(mode="after")
    def validate_trade(self) -> TransactionCreate:
        self.symbol = self.symbol.strip().upper()
        if self.transaction_type in {"buy", "sell", "rights"} and (
            self.quantity <= 0 or self.price <= 0
        ):
            raise ValueError("trade quantity and price must be positive")
        if self.transaction_type == "cash_balance" and self.symbol != "BDT":
            raise ValueError("cash-balance rows must use symbol BDT")
        return self


class HoldingView(BaseModel):
    symbol: str
    quantity: Decimal
    average_purchase_price: Decimal
    cost_basis: Decimal
    current_price: Decimal | None
    market_value: Decimal | None
    unrealized_pnl: Decimal | None
    unrealized_pnl_percent: Decimal | None
    realized_pnl: Decimal
    dividend_income: Decimal
    allocation_percent: Decimal | None = None


class PortfolioView(BaseModel):
    holdings: list[HoldingView]
    total_cost: Decimal
    total_market_value: Decimal | None
    total_unrealized_pnl: Decimal | None
    total_realized_pnl: Decimal
    dividend_income: Decimal
    cash: Decimal


class OrderProposalCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    idempotency_key: str = Field(min_length=8, max_length=128)
    symbol: str
    side: Literal["buy", "sell"]
    order_type: Literal["limit", "stop", "stop_limit", "trailing_stop", "market"] = "limit"
    quantity: int = Field(gt=0)
    limit_price: Decimal | None = Field(default=None, gt=0)
    stop_price: Decimal | None = Field(default=None, gt=0)
    current_price: Decimal = Field(gt=0)
    strategy_id: str | None = None
    expires_at: datetime | None = None
    data_timestamp: datetime
    data_quality_status: str = "valid"
    provider_disagreement_percent: Decimal | None = Field(default=None, ge=0)
    bid: Decimal | None = Field(default=None, ge=0)
    ask: Decimal | None = Field(default=None, ge=0)
    average_daily_volume: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_order_type(self) -> OrderProposalCreate:
        self.symbol = self.symbol.strip().upper()
        if self.order_type == "market":
            raise ValueError("market orders are disabled")
        if self.order_type in {"limit", "stop_limit"} and self.limit_price is None:
            raise ValueError("limit price is required")
        return self


class RiskDecision(BaseModel):
    approved: bool
    rejected: bool
    reason_codes: list[str]
    reasons: list[str]
    input_snapshot: dict[str, object]
    risk_rule_version: str
    timestamp: datetime


class BacktestRequest(BaseModel):
    symbol: str
    strategy: Literal["buy_hold", "ma_crossover", "momentum_dsex", "volume_breakout"]
    starting_capital: Decimal = Field(default=Decimal("1000000"), gt=0)
    fee_percent: Decimal = Field(default=Decimal("0.4"), ge=0)
    slippage_percent: Decimal = Field(default=Decimal("0.1"), ge=0)
    minimum_quantity: int = Field(default=1, gt=0)
    parameters: dict[str, float | int] = Field(default_factory=dict)
