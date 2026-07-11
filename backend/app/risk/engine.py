from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal

from app.schemas.trading import OrderProposalCreate, RiskDecision


@dataclass(frozen=True)
class RiskLimits:
    max_trade_value: Decimal = Decimal("250000")
    max_quantity: int = 100_000
    max_position_percent: Decimal = Decimal("20")
    max_orders_per_day: int = 20
    max_open_orders: int = 10
    min_average_daily_volume: int = 10_000
    max_provider_disagreement_percent: Decimal = Decimal("1")
    max_price_deviation_percent: Decimal = Decimal("2")
    max_spread_percent: Decimal = Decimal("1.5")
    restricted_symbols: tuple[str, ...] = ()
    approved_symbols: tuple[str, ...] = ()


class RiskEngine:
    version = "1.0.0"

    def __init__(self, limits: RiskLimits | None = None) -> None:
        self.limits = limits or RiskLimits()

    def evaluate(
        self,
        proposal: OrderProposalCreate,
        *,
        kill_switch_state: str,
        portfolio_value: Decimal,
        current_position_value: Decimal = Decimal("0"),
        orders_today: int = 0,
        open_orders: int = 0,
        data_age_seconds: float = 0,
        max_data_age_seconds: int = 30,
    ) -> RiskDecision:
        codes: list[str] = []
        reasons: list[str] = []

        def reject(code: str, reason: str) -> None:
            codes.append(code)
            reasons.append(reason)

        if kill_switch_state != "healthy":
            reject("KILL_SWITCH_NOT_HEALTHY", f"Kill switch state is {kill_switch_state}")
        if proposal.data_quality_status == "unsafe" or data_age_seconds > max_data_age_seconds:
            reject("STALE_OR_UNSAFE_DATA", "Market data is stale or unsafe")
        if (
            proposal.provider_disagreement_percent is not None
            and proposal.provider_disagreement_percent
            > self.limits.max_provider_disagreement_percent
        ):
            reject("PROVIDER_CONFLICT", "Provider disagreement exceeds the configured limit")
        value = proposal.quantity * (proposal.limit_price or proposal.current_price)
        if value > self.limits.max_trade_value:
            reject("MAX_TRADE_VALUE", "Trade value exceeds the configured maximum")
        if proposal.quantity > self.limits.max_quantity:
            reject("MAX_QUANTITY", "Quantity exceeds the configured maximum")
        if (
            portfolio_value > 0
            and (current_position_value + value) / portfolio_value * 100
            > self.limits.max_position_percent
        ):
            reject("MAX_POSITION_PERCENT", "Resulting position concentration is too high")
        if orders_today >= self.limits.max_orders_per_day:
            reject("MAX_DAILY_ORDERS", "Daily order count limit reached")
        if open_orders >= self.limits.max_open_orders:
            reject("MAX_OPEN_ORDERS", "Open-order limit reached")
        if (
            proposal.average_daily_volume is not None
            and proposal.average_daily_volume < self.limits.min_average_daily_volume
        ):
            reject("INSUFFICIENT_LIQUIDITY", "Average daily volume is below the minimum")
        if proposal.symbol in self.limits.restricted_symbols:
            reject("RESTRICTED_SYMBOL", "Symbol is restricted")
        if self.limits.approved_symbols and proposal.symbol not in self.limits.approved_symbols:
            reject("SYMBOL_NOT_APPROVED", "Symbol is not on the approved list")
        if proposal.limit_price:
            deviation = (
                abs(proposal.limit_price - proposal.current_price) / proposal.current_price * 100
            )
            if deviation > self.limits.max_price_deviation_percent:
                reject("PRICE_DEVIATION", "Limit price deviates too far from the current price")
        if proposal.bid and proposal.ask and proposal.ask > 0:
            spread = (proposal.ask - proposal.bid) / proposal.ask * 100
            if spread > self.limits.max_spread_percent:
                reject("SPREAD_TOO_WIDE", "Bid/ask spread exceeds the configured limit")
        snapshot = proposal.model_dump(mode="json") | {
            "limits": {key: str(value) for key, value in asdict(self.limits).items()},
            "portfolio_value": str(portfolio_value),
            "current_position_value": str(current_position_value),
        }
        return RiskDecision(
            approved=not codes,
            rejected=bool(codes),
            reason_codes=codes,
            reasons=reasons,
            input_snapshot=snapshot,
            risk_rule_version=self.version,
            timestamp=datetime.now(UTC),
        )
