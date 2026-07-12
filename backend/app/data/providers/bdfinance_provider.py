from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from app.data.providers.base import DataProviderError, MarketDataProvider
from app.schemas.market import (
    CompanyInfo,
    HistoricalBar,
    MarketSummary,
    NewsItem,
    ProviderCapability,
    Quote,
)


def _run[T](awaitable: Coroutine[Any, Any, T]) -> T:
    """Run the async-first provider from FastAPI's synchronous worker context."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(awaitable)
    raise DataProviderError("BDFinanceProvider sync adapter cannot run inside an event loop")


class BDFinanceProvider(MarketDataProvider):
    """Normalized sync facade for bdfinance's async BDStockClient."""

    name = "bdfinance"

    def get_capabilities(self) -> ProviderCapability:
        try:
            self._client_type()
            installed = True
        except Exception:
            installed = False

        return ProviderCapability(
            available=installed,
            authenticated=False,
            supports_quotes=installed,
            supports_history=installed,
            trustworthy_market_timestamp=False,
            supports_depth=installed,
            supports_news=installed,
            suitable_for_signals=installed,
            suitable_for_order_approval=False,
            limitation_reasons=["bdfinance package is not installed in python environment"]
            if not installed
            else ["bdfinance quotes lack exchange execution timestamps"],
        )

    @staticmethod
    def _client_type() -> Any:
        try:
            import bdfinance
            from bdfinance import BDStockClient
        except ImportError as exc:
            raise DataProviderError(
                "bdfinance is not installed; install the providers extra"
            ) from exc
        if getattr(bdfinance, "__DSE_AUTOTRADER_TYPE_FACADE__", False):
            raise DataProviderError(
                "Only the local bdfinance typing facade is present; no published runtime package is installed"
            )
        return BDStockClient

    async def _quote(self, symbol: str) -> Any:
        async with self._client_type()() as client:
            return await client.ticker(symbol.upper()).quote()

    def get_symbols(self) -> list[str]:
        raise DataProviderError(
            "bdfinance 0.x has no public symbol-list method; use cached primary-provider symbols"
        )

    def get_quote(self, symbol: str) -> Quote:
        row = _run(self._quote(symbol))
        if row is None:
            raise DataProviderError(f"No current bdfinance data for {symbol}")
        now = datetime.now(UTC)
        previous = Decimal(str(row.ycp))
        change = Decimal(str(row.change))
        return Quote(
            symbol=row.symbol,
            last_price=Decimal(str(row.ltp)),
            high=Decimal(str(row.high)),
            low=Decimal(str(row.low)),
            previous_close=previous,
            change=change,
            change_percent=change / previous * 100 if previous else None,
            volume=row.volume,
            trade_count=row.trade,
            turnover=Decimal(str(row.value)),
            market_timestamp=now,
            received_at=now,
            source=self.name,
            quality_status="unsafe",
            quality_flags=["market_timestamp_unavailable_received_time_used"],
            timestamp_provenance="receipt_only",
        )

    async def _history(self, symbol: str, start: date, end: date) -> Any:
        async with self._client_type()() as client:
            return await client.ticker(symbol.upper()).history(
                start=start.isoformat(), end=end.isoformat()
            )

    def get_history(self, symbol: str, start: date, end: date) -> list[HistoricalBar]:
        frame = _run(self._history(symbol, start, end))
        if frame is None or frame.empty:
            return []
        result: list[HistoricalBar] = []
        for _, row in frame.iterrows():
            timestamp = datetime.fromisoformat(str(row.get("date") or row.name)).replace(tzinfo=UTC)
            result.append(
                HistoricalBar(
                    timestamp=timestamp,
                    symbol=str(row.get("symbol") or symbol),
                    open=Decimal(str(row["open"])),
                    high=Decimal(str(row["high"])),
                    low=Decimal(str(row["low"])),
                    close=Decimal(str(row["close"])),
                    volume=int(row["volume"]) if row.get("volume") is not None else None,
                    trade_count=int(row["trade"]) if row.get("trade") is not None else None,
                    turnover=Decimal(str(row["value"])) if row.get("value") is not None else None,
                    source=self.name,
                    timestamp_provenance="provider_asserted",
                )
            )
        return sorted(result, key=lambda item: item.timestamp)

    def get_market_summary(self) -> MarketSummary:
        raise DataProviderError(
            "bdfinance 0.x has no public market-summary method; use the primary provider"
        )

    async def _info(self, symbol: str) -> Any:
        async with self._client_type()() as client:
            return await client.ticker(symbol.upper()).info(summary=True)

    def get_company_info(self, symbol: str) -> CompanyInfo:
        info = _run(self._info(symbol))
        if info is None:
            return CompanyInfo(symbol=symbol.upper())
        raw = info.model_dump(mode="json")
        basic = raw.get("basic_information", {})
        return CompanyInfo(
            symbol=symbol.upper(),
            name=basic.get("company_name"),
            sector=basic.get("sector"),
            fields=raw,
        )

    async def _depth(self, symbol: str) -> Any:
        async with self._client_type()() as client:
            return await client.ticker(symbol.upper()).depth()

    def get_market_depth(self, symbol: str) -> dict[str, object]:
        depth = _run(self._depth(symbol))
        return {
            "symbol": symbol.upper(),
            "available": depth is not None,
            **(depth.model_dump(mode="json") if depth is not None else {}),
        }

    async def _news(self, symbol: str) -> Any:
        async with self._client_type()() as client:
            return await client.ticker(symbol.upper()).news()

    def get_news(self, symbol: str | None = None) -> list[NewsItem]:
        if symbol is None:
            raise DataProviderError("bdfinance news requires a symbol")
        items = _run(self._news(symbol))
        result: list[NewsItem] = []
        for item in items:
            timestamp = datetime.fromisoformat(item.date) if item.date else datetime.now(UTC)
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=UTC)
            result.append(
                NewsItem(
                    timestamp=timestamp,
                    headline=item.title or "Untitled",
                    symbol=symbol.upper(),
                    source=self.name,
                )
            )
        return result

    def health_check(self) -> dict[str, object]:
        try:
            client = self._client_type()
            return {
                "provider": self.name,
                "healthy": all(hasattr(client, name) for name in ("ticker", "start", "close")),
                "contract": "BDStockClient async facade",
            }
        except Exception as exc:
            return {"provider": self.name, "healthy": False, "error": str(exc)}
