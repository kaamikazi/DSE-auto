from __future__ import annotations

import csv
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from app.data.providers.base import DataProviderError, MarketDataProvider
from app.schemas.market import (
    HistoricalBar,
    MarketSummary,
    ProviderCapability,
    Quote,
    TimestampProvenance,
)


class CSVProvider(MarketDataProvider):
    name = "csv"

    def __init__(self, root: Path) -> None:
        self.root = root

    def get_capabilities(self) -> ProviderCapability:
        return ProviderCapability(
            available=True,
            authenticated=True,
            supports_quotes=True,
            supports_history=True,
            trustworthy_market_timestamp=False,
            supports_depth=False,
            supports_news=False,
            suitable_for_signals=True,
            suitable_for_order_approval=False,
            limitation_reasons=["Generic CSV data is not operator-attested for order approval"],
        )

    def _path(self, symbol: str) -> Path:
        return self.root / f"{symbol.upper()}.csv"

    def get_symbols(self) -> list[str]:
        return (
            sorted(path.stem.upper() for path in self.root.glob("*.csv"))
            if self.root.exists()
            else []
        )

    def _rows(self, symbol: str) -> list[dict[str, str]]:
        path = self._path(symbol)
        if not path.exists():
            raise DataProviderError(f"CSV data not found for {symbol}")
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            return list(csv.DictReader(handle))

    @staticmethod
    def _decimal(row: dict[str, str], key: str, required: bool = True) -> Decimal | None:
        raw = (row.get(key) or "").strip()
        if not raw and not required:
            return None
        try:
            return Decimal(raw.replace(",", ""))
        except InvalidOperation as exc:
            raise DataProviderError(f"Invalid {key}: {raw!r}") from exc

    def get_history(self, symbol: str, start: date, end: date) -> list[HistoricalBar]:
        result: list[HistoricalBar] = []
        seen: set[datetime] = set()
        for row in self._rows(symbol):
            raw_timestamp = row.get("timestamp") or row.get("date")
            if not raw_timestamp:
                raise DataProviderError("Missing timestamp/date column")
            timestamp = datetime.fromisoformat(raw_timestamp)
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=UTC)
            if timestamp in seen:
                raise DataProviderError(f"Duplicate timestamp: {timestamp.isoformat()}")
            seen.add(timestamp)
            if start <= timestamp.date() <= end:
                open_price = self._decimal(row, "open")
                high = self._decimal(row, "high")
                low = self._decimal(row, "low")
                close = self._decimal(row, "close")
                if open_price is None or high is None or low is None or close is None:
                    raise DataProviderError("OHLC columns are required")
                result.append(
                    HistoricalBar(
                        timestamp=timestamp,
                        symbol=symbol,
                        open=open_price,
                        high=high,
                        low=low,
                        close=close,
                        volume=int(row["volume"].replace(",", "")) if row.get("volume") else None,
                        trade_count=int(row["trade_count"]) if row.get("trade_count") else None,
                        turnover=self._decimal(row, "turnover", required=False),
                        source=self.name,
                        timestamp_provenance=TimestampProvenance(
                            row.get("timestamp_provenance", "unknown")
                        ),
                    )
                )
        return sorted(result, key=lambda item: item.timestamp)

    def get_quote(self, symbol: str) -> Quote:
        rows = self.get_history(symbol, date.min, date.max)
        if not rows:
            raise DataProviderError(f"No CSV rows for {symbol}")
        bar = rows[-1]
        return Quote(
            symbol=bar.symbol,
            last_price=bar.close,
            open=bar.open,
            high=bar.high,
            low=bar.low,
            volume=bar.volume,
            trade_count=bar.trade_count,
            turnover=bar.turnover,
            market_timestamp=bar.timestamp,
            source=self.name,
            timestamp_provenance=bar.timestamp_provenance,
        )

    def get_market_summary(self) -> MarketSummary:
        quote = self.get_quote("DSEX")
        return MarketSummary(
            timestamp=quote.market_timestamp, index_value=quote.last_price, source=self.name
        )

    def health_check(self) -> dict[str, object]:
        return {
            "provider": self.name,
            "healthy": self.root.exists(),
            "symbols": len(self.get_symbols()),
        }


class OperatorAttestedCSVProvider(CSVProvider):
    name = "attested_csv"

    def get_capabilities(self) -> ProviderCapability:
        rows_attested = False
        for symbol in self.get_symbols():
            try:
                rows_attested = bool(self._rows(symbol)) and all(
                    row.get("timestamp_provenance") == "operator_attested"
                    for row in self._rows(symbol)
                )
            except DataProviderError:
                rows_attested = False
            if rows_attested:
                break
        return ProviderCapability(
            available=self.root.exists(),
            authenticated=True,
            supports_quotes=True,
            supports_history=True,
            trustworthy_market_timestamp=rows_attested,
            supports_depth=False,
            supports_news=False,
            suitable_for_signals=rows_attested,
            suitable_for_order_approval=rows_attested,
            limitation_reasons=[] if rows_attested else ["No operator-attested CSV rows found"],
        )

    def get_history(self, symbol: str, start: date, end: date) -> list[HistoricalBar]:
        result = super().get_history(symbol, start, end)
        if any(item.timestamp_provenance != "operator_attested" for item in result):
            raise DataProviderError(
                "Every attested CSV row must declare operator_attested provenance"
            )
        return result
