from pathlib import Path

from app.data.providers.base import MarketDataProvider
from app.data.providers.bdfinance_provider import BDFinanceProvider
from app.data.providers.bdshare_provider import BDShareProvider
from app.data.providers.csv_provider import CSVProvider
from app.data.providers.mock import MockProvider


def create_provider(name: str, csv_root: Path) -> MarketDataProvider:
    providers = {
        "mock": lambda: MockProvider(),
        "csv": lambda: CSVProvider(csv_root),
        "bdshare": BDShareProvider,
        "bdfinance": BDFinanceProvider,
    }
    try:
        return providers[name.lower()]()
    except KeyError as exc:
        raise ValueError(f"Unsupported data provider: {name}") from exc
