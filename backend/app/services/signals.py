from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from app.models import Signal
from app.schemas.market import HistoricalBar, Quote
from app.services.audit import append_audit


def moving_average_signal(
    db: Session, symbol: str, bars: list[HistoricalBar], quote: Quote
) -> Signal:
    if len(bars) < 50:
        raise ValueError("50 bars are required for the moving-average signal")
    closes = [bar.close for bar in sorted(bars, key=lambda item: item.timestamp)]
    fast = sum(closes[-20:], Decimal("0")) / 20
    slow = sum(closes[-50:], Decimal("0")) / 50
    strength = min(abs(float(fast / slow - 1)) * 1000, 100.0)
    signal_type = "buy_candidate" if fast > slow else "sell_candidate"
    signal = Signal(
        strategy_id="ma_crossover",
        strategy_version="1.0.0",
        symbol=symbol.upper(),
        signal_type=signal_type,
        strength_score=strength,
        entry_price=quote.last_price,
        stop_price=(quote.last_price * Decimal("0.92")).quantize(Decimal("0.1")),
        target_price=(quote.last_price * Decimal("1.15")).quantize(Decimal("0.1")),
        quantity_suggestion=None,
        reason_codes=["FAST_MA_ABOVE_SLOW_MA" if fast > slow else "FAST_MA_BELOW_SLOW_MA"],
        explanation=f"20-day average {fast:.2f}; 50-day average {slow:.2f}. Strength is a strategy score, not a probability.",
        source_data_snapshot={
            "quote": quote.model_dump(mode="json"),
            "fast_ma": str(fast),
            "slow_ma": str(slow),
        },
        data_quality_status=quote.quality_status.value,
        risk_preview={"requires_pretrade_check": True},
        expires_at=datetime.now(UTC) + timedelta(hours=24),
    )
    db.add(signal)
    db.flush()
    append_audit(
        db,
        actor="signal_engine",
        event_type="signal.generated",
        entity_type="signal",
        entity_id=signal.id,
        new_state={"type": signal_type, "symbol": symbol},
    )
    db.commit()
    return signal
