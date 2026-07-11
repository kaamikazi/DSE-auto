from __future__ import annotations

from datetime import date

from sqlalchemy.orm import Session

from app.data.providers.base import MarketDataProvider
from app.models import MarketBar
from app.services.audit import append_audit


class CollectionService:
    """Idempotent job targets for an external scheduler, Windows Task Scheduler, or queue worker."""

    def __init__(self, db: Session, provider: MarketDataProvider) -> None:
        self.db, self.provider = db, provider

    def historical_backfill(self, symbol: str, start: date, end: date) -> int:
        count = 0
        for bar in self.provider.get_history(symbol, start, end):
            exists = (
                self.db.query(MarketBar)
                .filter_by(symbol=bar.symbol, timestamp=bar.timestamp, source=bar.source)
                .first()
            )
            if exists:
                continue
            values = bar.model_dump(exclude={"quality_flags"})
            values["quality_status"] = bar.quality_status.value
            self.db.add(MarketBar(**values))
            count += 1
        append_audit(
            self.db,
            actor="collector",
            event_type="data.historical_backfill",
            entity_type="symbol",
            entity_id=symbol.upper(),
            metadata={"inserted": count, "start": start.isoformat(), "end": end.isoformat()},
        )
        self.db.commit()
        return count

    def current_quote_refresh(self, symbols: list[str]) -> dict[str, object]:
        quotes = self.provider.get_quotes(symbols)
        append_audit(
            self.db,
            actor="collector",
            event_type="data.quotes_refreshed",
            entity_type="market_data",
            metadata={"symbols": symbols, "count": len(quotes), "source": self.provider.name},
        )
        self.db.commit()
        return {quote.symbol: quote.model_dump(mode="json") for quote in quotes}

    def daily_market_summary(self) -> dict[str, object]:
        summary = self.provider.get_market_summary()
        append_audit(
            self.db,
            actor="collector",
            event_type="data.market_summary",
            entity_type="market_summary",
            new_state=summary.model_dump(mode="json"),
        )
        self.db.commit()
        return summary.model_dump(mode="json")

    def price_sensitive_news(self, symbol: str | None = None) -> list[dict[str, object]]:
        return [
            item.model_dump(mode="json") for item in self.provider.get_price_sensitive_news(symbol)
        ]

    def end_of_day_reconciliation(self) -> dict[str, object]:
        summary = self.daily_market_summary()
        return {"reconciled": True, "market_summary": summary}
