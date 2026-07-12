from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class ProviderCapabilities:
    quotes: bool = False
    symbols: bool = False
    history: bool = False
    market_summary: bool = False
    dsex: bool = False
    company_info: bool = False
    pe_ratio: bool = False
    market_depth: bool = False
    price_sensitive_news: bool = False
    exchange_timestamp: bool = False

    def to_dict(self) -> dict[str, bool]:
        return asdict(self)


CAPABILITIES = {
    "bdshare": ProviderCapabilities(
        True, True, True, True, False, False, False, False, True, False
    ),
    "bdfinance": ProviderCapabilities(
        True, True, True, False, True, True, True, True, False, False
    ),
    "csv": ProviderCapabilities(True, True, True, False, False, False, False, False, False, True),
    "mock": ProviderCapabilities(True, True, True, True, True, True, True, True, True, True),
}


def capabilities_for(provider_name: str) -> ProviderCapabilities:
    return CAPABILITIES.get(provider_name.lower(), ProviderCapabilities())
