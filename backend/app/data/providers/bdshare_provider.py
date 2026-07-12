from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from app.data.providers.base import DataProviderError, MarketDataProvider
from app.schemas.market import HistoricalBar, MarketSummary, ProviderCapability, Quote


class BDShareProvider(MarketDataProvider):
    """Optional bdshare adapter. Imports lazily so offline mode never depends on it."""

    name = "bdshare"

    def get_capabilities(self) -> ProviderCapability:
        return ProviderCapability(
            available=True,
            authenticated=False,
            supports_quotes=True,
            supports_history=True,
            trustworthy_market_timestamp=False,
            supports_depth=True,
            supports_news=True,
            suitable_for_signals=True,
            suitable_for_order_approval=False,
            limitation_reasons=["Public scraped DSE quotes lack exchange execution timestamps"],
        )

    @staticmethod
    def _module() -> Any:
        try:
            import bdshare  # type: ignore[import-untyped]
        except ImportError as exc:
            raise DataProviderError(
                "bdshare is not installed; install the providers extra"
            ) from exc
        return bdshare

    def get_symbols(self) -> list[str]:
        frame = self._module().get_current_trading_code()
        if frame is None or frame.empty or "symbol" not in frame:
            raise DataProviderError("bdshare returned no trading-code symbols")
        return sorted(
            {str(value).strip().upper() for value in frame["symbol"] if str(value).strip()}
        )

    def get_history(self, symbol: str, start: date, end: date) -> list[HistoricalBar]:
        frame = self._module().get_hist_data(start.isoformat(), end.isoformat(), symbol)
        if frame is None or frame.empty:
            return []
        result: list[HistoricalBar] = []
        for _, row in frame.iterrows():
            timestamp = datetime.fromisoformat(str(row.get("date") or row.name)).replace(tzinfo=UTC)
            result.append(
                HistoricalBar(
                    timestamp=timestamp,
                    symbol=symbol,
                    open=Decimal(str(row["open"])),
                    high=Decimal(str(row["high"])),
                    low=Decimal(str(row["low"])),
                    close=Decimal(str(row["close"])),
                    volume=int(row["volume"]) if row.get("volume") is not None else None,
                    trade_count=int(row["trade"]) if row.get("trade") is not None else None,
                    turnover=Decimal(str(row["value"])) if row.get("value") is not None else None,
                    source=self.name,
                )
            )
        return sorted(result, key=lambda item: item.timestamp)

    def get_quote(self, symbol: str) -> Quote:
        frame = self._module().get_current_trade_data(symbol.upper())
        if frame is None or frame.empty:
            raise DataProviderError(f"No current bdshare data for {symbol}")
        row = frame.iloc[0]
        now = datetime.now(UTC)
        last = Decimal(str(row.get("ltp") or row.get("close")))
        previous = Decimal(str(row["ycp"])) if row.get("ycp") is not None else None
        change = Decimal(str(row["change"])) if row.get("change") is not None else None
        return Quote(
            symbol=symbol,
            last_price=last,
            open=Decimal(str(row["open"])) if row.get("open") is not None else None,
            high=Decimal(str(row["high"])) if row.get("high") is not None else None,
            low=Decimal(str(row["low"])) if row.get("low") is not None else None,
            previous_close=previous,
            change=change,
            change_percent=(change / previous * 100) if change is not None and previous else None,
            volume=int(row["volume"]) if row.get("volume") is not None else None,
            trade_count=int(row["trade"]) if row.get("trade") is not None else None,
            turnover=Decimal(str(row["value"])) if row.get("value") is not None else None,
            # Public current-trade output has no exchange timestamp; this is explicit and unsafe.
            market_timestamp=now,
            received_at=now,
            source=self.name,
            quality_status="unsafe",
            quality_flags=["market_timestamp_unavailable_received_time_used"],
        )

    def get_market_summary(self) -> MarketSummary:
        frame = self._module().get_dsex_data("DSEX")
        if frame is None or frame.empty:
            raise DataProviderError("No current DSEX index data")
        row = frame.iloc[0]
        value = row.get("ltp") if row.get("ltp") is not None else row.get("close")
        return MarketSummary(
            timestamp=datetime.now(UTC),
            index_value=Decimal(str(value)) if value is not None else None,
            source=self.name,
        )

    def get_market_depth(self, symbol: str) -> dict[str, object]:
        frame = self._module().get_market_depth_data(symbol.upper())
        if frame is None or frame.empty:
            return {"symbol": symbol.upper(), "bids": [], "asks": [], "available": False}
        return {
            "symbol": symbol.upper(),
            "rows": frame.to_dict(orient="records"),
            "available": True,
        }

    def health_check(self) -> dict[str, object]:
        try:
            module = self._module()
            required = ("get_hist_data", "get_current_trade_data", "get_current_trading_code")
            missing = [name for name in required if not hasattr(module, name)]
            return {"provider": self.name, "healthy": not missing, "missing_contracts": missing}
        except Exception as exc:
            return {"provider": self.name, "healthy": False, "error": str(exc)}
