from collections.abc import Callable
from decimal import Decimal
from pathlib import Path

from app.core.config import get_settings
from app.data.providers.base import MarketDataProvider
from app.data.providers.bdfinance_provider import BDFinanceProvider
from app.data.providers.bdshare_provider import BDShareProvider
from app.data.providers.csv_provider import CSVProvider, OperatorAttestedCSVProvider
from app.data.providers.fake_certified import FakeCertifiedFeedAdapter
from app.data.providers.mock import MockProvider


def create_provider(name: str, csv_root: Path) -> MarketDataProvider:
    settings = get_settings()
    providers: dict[str, Callable[[], MarketDataProvider]] = {
        "mock": lambda: MockProvider(),
        "csv": lambda: CSVProvider(csv_root),
        "attested_csv": lambda: OperatorAttestedCSVProvider(csv_root),
        "bdshare": lambda: BDShareProvider(
            settings.BDSHARE_PRIMARY_ENDPOINT,
            settings.BDSHARE_SECONDARY_ENDPOINT,
            settings.DSE_CUSTOM_CA_BUNDLE,
        ),
        "bdfinance": lambda: BDFinanceProvider(),
        "fake_certified": lambda: FakeCertifiedFeedAdapter(),
    }
    name_lower = name.lower()
    if name_lower == "reliable":
        from app.data.providers.reliable import ReliableDataProvider

        prim_name = (
            settings.DATA_PRIMARY_PROVIDER
            if settings.DATA_PRIMARY_PROVIDER.lower() != "reliable"
            else "mock"
        )
        sec_name = (
            settings.DATA_SECONDARY_PROVIDER
            if settings.DATA_SECONDARY_PROVIDER.lower() != "reliable"
            else "csv"
        )
        primary = providers.get(prim_name.lower(), lambda: MockProvider())()
        secondary = providers.get(sec_name.lower(), lambda: CSVProvider(csv_root))()
        return ReliableDataProvider(
            primary,
            secondary,
            max_disagreement_percent=Decimal(str(settings.DATA_MAX_PROVIDER_DISAGREEMENT_PERCENT)),
            max_staleness_seconds=settings.DATA_MAX_STALENESS_SECONDS,
        )
    try:
        return providers[name_lower]()
    except KeyError as exc:
        raise ValueError(f"Unsupported data provider: {name}") from exc
