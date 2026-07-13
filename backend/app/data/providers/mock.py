from __future__ import annotations

import hashlib
import math
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

from app.data.providers.base import DataProviderError, MarketDataProvider
from app.schemas.market import (
    HistoricalBar,
    MarketSummary,
    ProviderCapability,
    QualityStatus,
    Quote,
    TimestampProvenance,
)

SYMBOLS = ["GP", "SQURPHARMA", "BRACBANK", "BATBC", "ACI", "RENATA", "CITYBANK", "BEXIMCO", "DSEX"]


class MockProvider(MarketDataProvider):
    name = "mock"

    def __init__(self, now: datetime | None = None, stale: bool = False) -> None:
        self.now = now or datetime.now(UTC)
        self.stale = stale

    def get_capabilities(self) -> ProviderCapability:
        from app.core.config import get_settings

        settings = get_settings()
        # suitable for order approval only inside test mode
        suitable = settings.APP_ENV == "test" or getattr(settings, "ALLOW_MOCK_APPROVALS", False)
        return ProviderCapability(
            available=True,
            authenticated=True,
            supports_quotes=True,
            supports_history=True,
            trustworthy_market_timestamp=True,
            supports_depth=True,
            supports_news=True,
            suitable_for_signals=True,
            suitable_for_order_approval=suitable,
            limitation_reasons=[]
            if suitable
            else ["Mock data cannot approve orders outside test mode"],
        )

    def get_symbols(self) -> list[str]:
        return SYMBOLS[:-1]

    @staticmethod
    def _base(symbol: str) -> Decimal:
        digest = hashlib.sha256(symbol.upper().encode()).digest()
        return Decimal(50 + int.from_bytes(digest[:2]) % 450)

    def get_quote(self, symbol: str) -> Quote:
        symbol = symbol.upper()
        if symbol not in SYMBOLS:
            raise DataProviderError(f"Unknown mock symbol: {symbol}")
        base = self._base(symbol)
        last = (base * Decimal("1.012")).quantize(Decimal("0.1"))
        timestamp = self.now - timedelta(minutes=10) if self.stale else self.now
        return Quote(
            symbol=symbol,
            last_price=last,
            open=base,
            high=last * Decimal("1.01"),
            low=base * Decimal("0.99"),
            previous_close=base,
            change=last - base,
            change_percent=(last / base - 1) * 100,
            volume=100_000 + int(base) * 100,
            trade_count=500,
            turnover=last * 100_000,
            bid=last - Decimal("0.1"),
            ask=last + Decimal("0.1"),
            market_timestamp=timestamp,
            received_at=self.now,
            source=self.name,
            stale=self.stale,
            quality_status=QualityStatus.UNSAFE if self.stale else QualityStatus.VALID,
            quality_flags=["stale"] if self.stale else [],
            timestamp_provenance=TimestampProvenance.EXCHANGE_VERIFIED,
        )

    def get_history(self, symbol: str, start: date, end: date) -> list[HistoricalBar]:
        if end < start:
            raise DataProviderError("end date precedes start date")
        symbol = symbol.upper()
        base = float(self._base(symbol))
        rows: list[HistoricalBar] = []
        day = start
        index = 0
        while day <= end:
            if day.weekday() < 5:
                trend = 1 + index * 0.0008
                wave = math.sin(index / 8) * 0.025
                close = Decimal(str(round(base * (trend + wave), 2)))
                open_price = close * Decimal("0.997")
                rows.append(
                    HistoricalBar(
                        timestamp=datetime.combine(day, time(14, 30), tzinfo=UTC),
                        symbol=symbol,
                        open=open_price,
                        high=max(open_price, close) * Decimal("1.01"),
                        low=min(open_price, close) * Decimal("0.99"),
                        close=close,
                        volume=80_000 + (index % 20) * 8_000,
                        trade_count=300 + index % 100,
                        turnover=close * (80_000 + (index % 20) * 8_000),
                        source=self.name,
                        timestamp_provenance=TimestampProvenance.EXCHANGE_VERIFIED,
                    )
                )
                index += 1
            day += timedelta(days=1)
        return rows

    def get_market_summary(self) -> MarketSummary:
        return MarketSummary(
            timestamp=self.now,
            index_value=Decimal("5275.40"),
            total_volume=21_000_000,
            source=self.name,
        )

    def health_check(self) -> dict[str, object]:
        return {"provider": self.name, "healthy": True, "offline_capable": True}
