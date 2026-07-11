from datetime import UTC, datetime
from decimal import Decimal

from app.schemas.market import DataComparison, QualityStatus, Quote


def compare_quotes(
    primary: Quote,
    secondary: Quote | None,
    *,
    max_disagreement_percent: Decimal,
    max_staleness_seconds: int,
    now: datetime | None = None,
) -> DataComparison:
    now = now or datetime.now(UTC)
    reasons: list[str] = []
    age = (now - primary.market_timestamp).total_seconds()
    if primary.stale or age > max_staleness_seconds:
        reasons.append("STALE_PRIMARY_DATA")
    if primary.quality_status == QualityStatus.UNSAFE:
        reasons.append("PRIMARY_DATA_UNSAFE")
    disagreement: Decimal | None = None
    if secondary is not None:
        secondary_age = (now - secondary.market_timestamp).total_seconds()
        if secondary.stale or secondary_age > max_staleness_seconds:
            reasons.append("STALE_SECONDARY_DATA")
        denominator = max(abs(primary.last_price), abs(secondary.last_price), Decimal("0.0001"))
        disagreement = abs(primary.last_price - secondary.last_price) / denominator * 100
        if disagreement > max_disagreement_percent:
            reasons.append("PROVIDER_PRICE_CONFLICT")
        if primary.previous_close and secondary.previous_close:
            previous_diff = (
                abs(primary.previous_close - secondary.previous_close)
                / max(primary.previous_close, secondary.previous_close)
                * 100
            )
            if previous_diff > max_disagreement_percent:
                reasons.append("PROVIDER_PREVIOUS_CLOSE_CONFLICT")
    else:
        reasons.append("SECONDARY_PROVIDER_UNAVAILABLE")
    blocking = {
        "STALE_PRIMARY_DATA",
        "PRIMARY_DATA_UNSAFE",
        "PROVIDER_PRICE_CONFLICT",
        "PROVIDER_PREVIOUS_CLOSE_CONFLICT",
    }
    return DataComparison(
        symbol=primary.symbol,
        safe_for_orders=not bool(blocking.intersection(reasons)),
        disagreement_percent=disagreement,
        reason_codes=reasons,
        primary=primary,
        secondary=secondary,
    )
