from __future__ import annotations

from datetime import UTC, datetime

from app.core.config import get_settings
from app.data.adapters.sdk import AdapterDescriptor, AdapterHealth, DataAdapter
from app.data.providers.mock import MockProvider
from app.schemas.market import ProviderCapability, TimestampProvenance


class FakeCertifiedFeedAdapter(MockProvider, DataAdapter):
    """Deterministic full-capability feed used only for contract/integration tests."""

    name = "fake_certified"

    def descriptor(self) -> AdapterDescriptor:
        return AdapterDescriptor(
            adapter_id=self.name,
            capabilities=(
                "streaming_quotes",
                "polling_quotes",
                "historical_data",
                "dsex_index",
                "market_depth",
                "corporate_actions",
                "price_sensitive_news",
            ),
            timestamp_source="deterministic simulated exchange clock",
            timestamp_trust=TimestampProvenance.EXCHANGE_VERIFIED,
            update_frequency_seconds=1,
            estimated_latency_ms=5,
            licensing_status="test_only",
            authentication_method="none",
            rate_limit_per_minute=None,
        )

    def adapter_health(self) -> AdapterHealth:
        return AdapterHealth(
            state="healthy", checked_at=datetime.now(UTC).isoformat(), latency_ms=5
        )

    def get_capabilities(self) -> ProviderCapability:
        test_only = get_settings().APP_ENV == "test"
        return ProviderCapability(
            available=True,
            authenticated=True,
            supports_quotes=True,
            supports_history=True,
            trustworthy_market_timestamp=True,
            supports_depth=True,
            supports_news=True,
            suitable_for_signals=test_only,
            suitable_for_order_approval=test_only,
            limitation_reasons=[]
            if test_only
            else ["Fake certified feed is test-only and cannot approve operational paper orders"],
        )

    def health_check(self) -> dict[str, object]:
        return {
            "provider": self.name,
            "healthy": True,
            "test_only": True,
            "descriptor": self.descriptor().to_dict(),
        }
