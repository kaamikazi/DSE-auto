from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class QualityStatus(StrEnum):
    VALID = "valid"
    DEGRADED = "degraded"
    UNSAFE = "unsafe"


class TimestampProvenance(StrEnum):
    EXCHANGE_VERIFIED = "exchange_verified"
    PROVIDER_ASSERTED = "provider_asserted"
    OPERATOR_ATTESTED = "operator_attested"
    RECEIPT_ONLY = "receipt_only"
    UNKNOWN = "unknown"


class HistoricalBar(BaseModel):
    model_config = ConfigDict(extra="forbid")
    timestamp: datetime
    symbol: str = Field(min_length=1, max_length=32)
    open: Decimal = Field(ge=0)
    high: Decimal = Field(ge=0)
    low: Decimal = Field(ge=0)
    close: Decimal = Field(ge=0)
    volume: int | None = Field(default=None, ge=0)
    trade_count: int | None = Field(default=None, ge=0)
    turnover: Decimal | None = Field(default=None, ge=0)
    source: str
    received_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    quality_status: QualityStatus = QualityStatus.VALID
    quality_flags: list[str] = Field(default_factory=list)
    timestamp_provenance: TimestampProvenance = TimestampProvenance.UNKNOWN

    @model_validator(mode="after")
    def validate_range(self) -> HistoricalBar:
        if self.high < self.low:
            raise ValueError("high must be greater than or equal to low")
        if not self.low <= self.open <= self.high:
            raise ValueError("open must be inside high-low range")
        if not self.low <= self.close <= self.high:
            raise ValueError("close must be inside high-low range")
        if self.volume == 0 and "zero_volume" not in self.quality_flags:
            self.quality_flags.append("zero_volume")
            self.quality_status = QualityStatus.DEGRADED
        self.symbol = self.symbol.strip().upper()
        return self


class Quote(BaseModel):
    model_config = ConfigDict(extra="forbid")
    symbol: str
    last_price: Decimal = Field(ge=0)
    open: Decimal | None = Field(default=None, ge=0)
    high: Decimal | None = Field(default=None, ge=0)
    low: Decimal | None = Field(default=None, ge=0)
    previous_close: Decimal | None = Field(default=None, ge=0)
    change: Decimal | None = None
    change_percent: Decimal | None = None
    volume: int | None = Field(default=None, ge=0)
    trade_count: int | None = Field(default=None, ge=0)
    turnover: Decimal | None = Field(default=None, ge=0)
    bid: Decimal | None = Field(default=None, ge=0)
    ask: Decimal | None = Field(default=None, ge=0)
    market_timestamp: datetime
    received_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    source: str
    stale: bool = False
    quality_status: QualityStatus = QualityStatus.VALID
    quality_flags: list[str] = Field(default_factory=list)
    timestamp_provenance: TimestampProvenance = TimestampProvenance.UNKNOWN

    @model_validator(mode="after")
    def validate_quote(self) -> Quote:
        if self.high is not None and self.low is not None and self.high < self.low:
            raise ValueError("high must be greater than or equal to low")
        self.symbol = self.symbol.strip().upper()
        return self


class DataComparison(BaseModel):
    symbol: str
    safe_for_orders: bool
    disagreement_percent: Decimal | None
    reason_codes: list[str]
    primary: Quote
    secondary: Quote | None


class MarketSummary(BaseModel):
    timestamp: datetime
    index_name: str = "DSEX"
    index_value: Decimal | None = None
    total_volume: int | None = None
    total_turnover: Decimal | None = None
    advancing: int | None = None
    declining: int | None = None
    unchanged: int | None = None
    source: str


class CompanyInfo(BaseModel):
    symbol: str
    name: str | None = None
    sector: str | None = None
    fields: dict[str, object] = Field(default_factory=dict)


class NewsItem(BaseModel):
    timestamp: datetime
    headline: str
    symbol: str | None = None
    url: str | None = None
    source: str
    price_sensitive: bool = False


class ProviderCapability(BaseModel):
    available: bool
    authenticated: bool
    supports_quotes: bool
    supports_history: bool
    trustworthy_market_timestamp: bool
    supports_depth: bool
    supports_news: bool
    suitable_for_signals: bool
    suitable_for_order_approval: bool
    limitation_reasons: list[str]
