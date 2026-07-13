from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from typing import Literal

from app.data.providers.base import MarketDataProvider
from app.schemas.market import TimestampProvenance

AdapterCapability = Literal[
    "streaming_quotes",
    "polling_quotes",
    "historical_data",
    "dsex_index",
    "market_depth",
    "corporate_actions",
    "price_sensitive_news",
]


@dataclass(frozen=True)
class AdapterHealth:
    state: Literal["healthy", "degraded", "unavailable", "unknown"]
    checked_at: str
    latency_ms: float | None = None
    details: str = ""


@dataclass(frozen=True)
class AdapterDescriptor:
    adapter_id: str
    capabilities: tuple[AdapterCapability, ...]
    timestamp_source: str
    timestamp_trust: TimestampProvenance
    update_frequency_seconds: float
    estimated_latency_ms: float
    licensing_status: Literal["licensed", "evaluation", "test_only", "unknown"]
    authentication_method: Literal["none", "api_key", "oauth2", "mutual_tls", "vendor_sdk"]
    rate_limit_per_minute: int | None
    documentation_url: str | None = None

    def to_dict(self) -> dict[str, object]:
        result = asdict(self)
        result["timestamp_trust"] = self.timestamp_trust.value
        return result


class DataAdapter(MarketDataProvider, ABC):
    """Vendor-neutral contract layered over the established provider API."""

    @abstractmethod
    def descriptor(self) -> AdapterDescriptor: ...

    @abstractmethod
    def adapter_health(self) -> AdapterHealth: ...

    def corporate_actions(self, symbol: str) -> list[dict[str, object]]:
        del symbol
        return []
