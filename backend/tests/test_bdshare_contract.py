from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pandas as pd  # type: ignore[import-untyped]

from app.data.providers.bdshare_provider import BDShareProvider


def test_bdshare_package_contract_methods() -> None:
    """Verify that the installed bdshare package has the required public API contract."""
    import bdshare  # type: ignore[import-untyped]

    assert hasattr(bdshare, "get_current_trading_code")
    assert hasattr(bdshare, "get_hist_data")
    assert hasattr(bdshare, "get_current_trade_data")
    assert hasattr(bdshare, "get_dsex_data")
    assert hasattr(bdshare, "get_market_depth_data")


@patch("bdshare.get_current_trading_code")
def test_bdshare_get_symbols_contract(mock_get_symbols: MagicMock) -> None:
    # Sanitized sample response for get_current_trading_code
    sample_df = pd.DataFrame({"symbol": ["GP", "BRACBANK", "SQURPHARMA"]})
    mock_get_symbols.return_value = sample_df

    provider = BDShareProvider()
    symbols = provider.get_symbols()
    assert symbols == ["BRACBANK", "GP", "SQURPHARMA"]
    mock_get_symbols.assert_called_once()


@patch("bdshare.get_hist_data")
def test_bdshare_get_history_contract(mock_get_hist: MagicMock) -> None:
    # Sanitized sample response for get_hist_data
    sample_df = pd.DataFrame(
        {
            "date": ["2026-07-10", "2026-07-11"],
            "open": [150.0, 151.0],
            "high": [152.0, 153.0],
            "low": [149.0, 150.0],
            "close": [151.5, 152.5],
            "volume": [5000, 6000],
            "trade": [120, 150],
            "value": [757500.0, 915000.0],
        }
    )
    mock_get_hist.return_value = sample_df

    provider = BDShareProvider()
    history = provider.get_history("GP", date(2026, 7, 10), date(2026, 7, 11))

    assert len(history) == 2
    assert history[0].symbol == "GP"
    assert history[0].open == Decimal("150.0")
    assert history[0].high == Decimal("152.0")
    assert history[0].low == Decimal("149.0")
    assert history[0].close == Decimal("151.5")
    assert history[0].volume == 5000
    assert history[0].trade_count == 120
    assert history[0].turnover == Decimal("757500.0")
    mock_get_hist.assert_called_once_with("2026-07-10", "2026-07-11", "GP")


@patch("bdshare.get_current_trade_data")
def test_bdshare_get_quote_contract_safety_check(mock_get_trade: MagicMock) -> None:
    # Sanitized sample response for get_current_trade_data
    # Public trade price scroll does not include an exchange execution timestamp
    sample_df = pd.DataFrame(
        {
            "symbol": ["GP"],
            "ltp": [152.5],
            "open": [150.0],
            "high": [153.0],
            "low": [149.0],
            "ycp": [151.0],
            "change": [1.5],
            "volume": [5000],
            "trade": [120],
            "value": [757500.0],
        }
    )
    mock_get_trade.return_value = sample_df

    provider = BDShareProvider()
    quote = provider.get_quote("GP")

    # Assert quote values
    assert quote.symbol == "GP"
    assert quote.last_price == Decimal("152.5")
    assert quote.open == Decimal("150.0")
    assert quote.high == Decimal("153.0")
    assert quote.low == Decimal("149.0")
    assert quote.previous_close == Decimal("151.0")
    assert quote.change == Decimal("1.5")
    assert quote.volume == 5000

    # CRITICAL: Verify safety properties
    assert quote.quality_status == "unsafe"
    assert "market_timestamp_unavailable_received_time_used" in quote.quality_flags
    # Assert capability states
    caps = provider.get_capabilities()
    assert caps.suitable_for_order_approval is False
    assert "Public scraped DSE quotes lack exchange execution timestamps" in caps.limitation_reasons
