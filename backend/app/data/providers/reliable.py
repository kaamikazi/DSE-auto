from __future__ import annotations

import logging
import time
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, cast

from app.data.providers.base import DataProviderError, MarketDataProvider
from app.schemas.market import (
    CompanyInfo,
    HistoricalBar,
    MarketSummary,
    NewsItem,
    QualityStatus,
    Quote,
)
from app.services.data_validation import compare_quotes

logger = logging.getLogger(__name__)


class CircuitBreaker:
    def __init__(
        self, name: str, failure_threshold: int = 3, cooldown_seconds: float = 60.0
    ) -> None:
        self.name = name
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds
        self.failure_count = 0
        self.last_failure_time = 0.0
        self.state = "closed"  # closed, open, half_open

    def record_success(self) -> None:
        self.failure_count = 0
        self.state = "closed"

    def record_failure(self) -> None:
        self.failure_count += 1
        self.last_failure_time = time.time()
        if self.failure_count >= self.failure_threshold:
            self.state = "open"
            logger.warning(
                "Circuit breaker for provider %s is now OPEN. Cooldown: %ss",
                self.name,
                self.cooldown_seconds,
            )

    def can_execute(self) -> bool:
        if self.state == "closed":
            return True
        if self.state == "open":
            if time.time() - self.last_failure_time > self.cooldown_seconds:
                self.state = "half_open"
                logger.info(
                    "Circuit breaker for provider %s is now HALF-OPEN. Testing next request.",
                    self.name,
                )
                return True
            return False
        return self.state == "half_open"


class ReliableDataProvider(MarketDataProvider):
    """Reliable wrapper that manages primary and secondary providers with circuit breakers."""

    name = "reliable"

    def __init__(
        self,
        primary: MarketDataProvider,
        secondary: MarketDataProvider,
        *,
        failure_threshold: int = 3,
        cooldown_seconds: float = 60.0,
        max_disagreement_percent: Decimal = Decimal("1.0"),
        max_staleness_seconds: int = 30,
    ) -> None:
        self.primary = primary
        self.secondary = secondary
        self.primary_breaker = CircuitBreaker(
            primary.name, failure_threshold, cooldown_seconds
        )
        self.secondary_breaker = CircuitBreaker(
            secondary.name, failure_threshold, cooldown_seconds
        )
        self.max_disagreement_percent = max_disagreement_percent
        self.max_staleness_seconds = max_staleness_seconds

    def _execute(self, method_name: str, *args: Any, **kwargs: Any) -> Any:
        primary_tried = False
        if self.primary_breaker.can_execute():
            primary_tried = True
            try:
                method = getattr(self.primary, method_name)
                result = method(*args, **kwargs)
                self.primary_breaker.record_success()
                return result
            except Exception as exc:
                self.primary_breaker.record_failure()
                logger.error(
                    "Primary provider %s error on %s: %s. Failing over...",
                    self.primary.name,
                    method_name,
                    exc,
                )

        if self.secondary_breaker.can_execute():
            try:
                method = getattr(self.secondary, method_name)
                result = method(*args, **kwargs)
                self.secondary_breaker.record_success()
                return result
            except Exception as exc:
                self.secondary_breaker.record_failure()
                logger.error(
                    "Secondary provider %s error on %s: %s.",
                    self.secondary.name,
                    method_name,
                    exc,
                )
                raise DataProviderError(
                    f"Both primary and secondary providers failed: {exc}"
                ) from exc

        raise DataProviderError(
            f"All providers are currently offline/circuits open. Primary tried={primary_tried}"
        )

    def get_symbols(self) -> list[str]:
        return cast(list[str], self._execute("get_symbols"))

    def get_quote(self, symbol: str) -> Quote:
        primary_quote: Quote | None = None
        secondary_quote: Quote | None = None
        now = datetime.now(UTC)

        # 1. Try Primary
        if self.primary_breaker.can_execute():
            try:
                primary_quote = self.primary.get_quote(symbol)
                self.primary_breaker.record_success()
            except Exception as exc:
                self.primary_breaker.record_failure()
                logger.error("Primary quote fetch failed for %s: %s", symbol, exc)

        # 2. Try Secondary (always fetched when available for dual-validation, or as fallback)
        if self.secondary_breaker.can_execute():
            try:
                secondary_quote = self.secondary.get_quote(symbol)
                self.secondary_breaker.record_success()
            except Exception as exc:
                self.secondary_breaker.record_failure()
                logger.error("Secondary quote fetch failed for %s: %s", symbol, exc)

        # 3. Handle Failures & Comparisons
        if primary_quote is None and secondary_quote is None:
            raise DataProviderError(f"Failed to fetch quotes from all providers for {symbol}")

        if primary_quote is not None:
            # We have primary, validate with secondary if available
            comparison = compare_quotes(
                primary_quote,
                secondary_quote,
                max_disagreement_percent=self.max_disagreement_percent,
                max_staleness_seconds=self.max_staleness_seconds,
                now=now,
            )
            # If quote comparison failed, mark the primary quote as unsafe
            if not comparison.safe_for_orders:
                primary_quote = primary_quote.model_copy(
                    update={
                        "quality_status": QualityStatus.UNSAFE,
                        "quality_flags": primary_quote.quality_flags
                        + comparison.reason_codes,
                    }
                )
            return primary_quote

        # Fallback to secondary quote since primary failed
        assert secondary_quote is not None
        # Mark secondary quote as degraded or unsafe if it has no companion to validate against
        flags = secondary_quote.quality_flags + ["primary_provider_failed"]
        return secondary_quote.model_copy(
            update={"quality_status": QualityStatus.DEGRADED, "quality_flags": flags}
        )

    def get_history(self, symbol: str, start: date, end: date) -> list[HistoricalBar]:
        return cast(list[HistoricalBar], self._execute("get_history", symbol, start, end))

    def get_market_summary(self) -> MarketSummary:
        return cast(MarketSummary, self._execute("get_market_summary"))

    def get_company_info(self, symbol: str) -> CompanyInfo:
        try:
            return cast(CompanyInfo, self._execute("get_company_info", symbol))
        except Exception:
            return CompanyInfo(symbol=symbol.upper())

    def get_market_depth(self, symbol: str) -> dict[str, object]:
        try:
            return cast(dict[str, object], self._execute("get_market_depth", symbol))
        except Exception:
            return {"symbol": symbol.upper(), "bids": [], "asks": [], "available": False}

    def get_news(self, symbol: str | None = None) -> list[NewsItem]:
        try:
            return cast(list[NewsItem], self._execute("get_news", symbol))
        except Exception:
            return []

    def health_check(self) -> dict[str, object]:
        return {
            "provider": self.name,
            "healthy": self.primary_breaker.state != "open"
            or self.secondary_breaker.state != "open",
            "primary": {
                "name": self.primary.name,
                "state": self.primary_breaker.state,
                "failures": self.primary_breaker.failure_count,
            },
            "secondary": {
                "name": self.secondary.name,
                "state": self.secondary_breaker.state,
                "failures": self.secondary_breaker.failure_count,
            },
        }
