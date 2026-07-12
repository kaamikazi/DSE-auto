from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date
from decimal import Decimal

from app.schemas.market import (
    CompanyInfo,
    HistoricalBar,
    MarketSummary,
    NewsItem,
    ProviderCapability,
    Quote,
)


class DataProviderError(RuntimeError):
    pass


class MarketDataProvider(ABC):
    name: str

    @abstractmethod
    def get_capabilities(self) -> ProviderCapability: ...

    @abstractmethod
    def get_symbols(self) -> list[str]: ...

    @abstractmethod
    def get_quote(self, symbol: str) -> Quote: ...

    def get_quotes(self, symbols: list[str]) -> list[Quote]:
        return [self.get_quote(symbol) for symbol in symbols]

    @abstractmethod
    def get_history(self, symbol: str, start: date, end: date) -> list[HistoricalBar]: ...

    @abstractmethod
    def get_market_summary(self) -> MarketSummary: ...

    def get_index_history(self, index_name: str, start: date, end: date) -> list[HistoricalBar]:
        return self.get_history(index_name, start, end)

    def get_company_info(self, symbol: str) -> CompanyInfo:
        return CompanyInfo(symbol=symbol.upper())

    def get_pe_ratios(self) -> dict[str, Decimal | None]:
        return {}

    def get_market_depth(self, symbol: str) -> dict[str, object]:
        return {"symbol": symbol.upper(), "bids": [], "asks": [], "available": False}

    def get_news(self, symbol: str | None = None) -> list[NewsItem]:
        return []

    def get_price_sensitive_news(self, symbol: str | None = None) -> list[NewsItem]:
        return [item for item in self.get_news(symbol) if item.price_sensitive]

    @abstractmethod
    def health_check(self) -> dict[str, object]: ...
