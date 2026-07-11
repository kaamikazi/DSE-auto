from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from app.data.providers.base import DataProviderError
from app.data.providers.bdfinance_provider import BDFinanceProvider
from app.data.providers.bdshare_provider import BDShareProvider
from app.data.providers.csv_provider import CSVProvider
from app.data.providers.mock import MockProvider
from app.schemas.market import HistoricalBar
from app.services.data_validation import compare_quotes


def test_mock_provider_is_deterministic() -> None:
    now = datetime(2025, 1, 1, tzinfo=UTC)
    assert MockProvider(now).get_quote("GP") == MockProvider(now).get_quote("GP")


def test_history_range_is_valid() -> None:
    bars = MockProvider().get_history("GP", date(2025, 1, 1), date(2025, 2, 1))
    assert bars
    assert all(bar.high >= bar.low and bar.low <= bar.close <= bar.high for bar in bars)


def test_invalid_price_range_rejected() -> None:
    with pytest.raises(ValueError):
        HistoricalBar(
            timestamp=datetime.now(UTC),
            symbol="GP",
            open=100,
            high=90,
            low=95,
            close=96,
            source="test",
        )


def test_stale_data_blocks_orders() -> None:
    now = datetime.now(UTC)
    comparison = compare_quotes(
        MockProvider(now, stale=True).get_quote("GP"),
        None,
        max_disagreement_percent=Decimal("1"),
        max_staleness_seconds=30,
        now=now,
    )
    assert comparison.safe_for_orders is False
    assert "STALE_PRIMARY_DATA" in comparison.reason_codes


def test_provider_conflict_blocks_orders() -> None:
    now = datetime.now(UTC)
    one = MockProvider(now).get_quote("GP")
    two = one.model_copy(update={"last_price": one.last_price * 2, "source": "secondary"})
    result = compare_quotes(
        one, two, max_disagreement_percent=Decimal("1"), max_staleness_seconds=30, now=now
    )
    assert not result.safe_for_orders
    assert "PROVIDER_PRICE_CONFLICT" in result.reason_codes


def test_csv_duplicate_timestamp_rejected(tmp_path) -> None:  # type: ignore[no-untyped-def]
    (tmp_path / "GP.csv").write_text(
        "timestamp,open,high,low,close,volume\n2025-01-01,10,12,9,11,100\n2025-01-01,10,12,9,11,100\n",
        encoding="utf-8",
    )
    with pytest.raises(DataProviderError, match="Duplicate"):
        CSVProvider(tmp_path).get_history("GP", date(2025, 1, 1), date(2025, 1, 2))


def test_csv_missing_column_rejected(tmp_path) -> None:  # type: ignore[no-untyped-def]
    (tmp_path / "GP.csv").write_text(
        "timestamp,open,high,low\n2025-01-01,10,12,9\n", encoding="utf-8"
    )
    with pytest.raises(DataProviderError):
        CSVProvider(tmp_path).get_history("GP", date.min, date.max)


def test_installed_real_provider_contracts_are_recognized() -> None:
    assert BDShareProvider().health_check()["healthy"] is True
    assert BDFinanceProvider().health_check()["healthy"] is True
