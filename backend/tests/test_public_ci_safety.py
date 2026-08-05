from __future__ import annotations

from decimal import Decimal
from typing import cast

import pytest
from pydantic import ValidationError

from app.brokers import OfficialBrokerAdapter
from app.core.config import Settings, assert_paper_only_safety
from app.core.database_identity import OPERATIONAL_SQLITE_PATH, sqlite_path_from_url
from app.models import Order


def _isolated_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "APP_ENV": "test",
        "DATABASE_URL": "sqlite:////tmp/dse-public-ci-test.db",
        "DATABASE_ROLE": "test",
        "ALLOW_DATABASE_ROLE_OVERRIDE": False,
        "TRADING_MODE": "paper",
        "LIVE_TRADING_ENABLED": False,
        "BROKER_ADAPTER": "disabled",
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def test_public_ci_configuration_is_isolated_and_paper_only() -> None:
    settings = Settings()
    assert settings.APP_ENV == "test"
    assert settings.DATABASE_ROLE == "test"
    assert settings.ALLOW_DATABASE_ROLE_OVERRIDE is False
    assert sqlite_path_from_url(settings.DATABASE_URL) != OPERATIONAL_SQLITE_PATH
    assert_paper_only_safety(settings)


def test_live_safety_weakening_is_rejected() -> None:
    with pytest.raises(ValidationError, match="Live trading is disabled"):
        _isolated_settings(TRADING_MODE="live")
    with pytest.raises(ValidationError, match="Live trading is disabled"):
        _isolated_settings(LIVE_TRADING_ENABLED=True)
    with pytest.raises(RuntimeError, match="Paper-only safety mismatch"):
        assert_paper_only_safety(_isolated_settings(BROKER_ADAPTER="paper"))


def test_official_broker_adapter_remains_nonfunctional() -> None:
    adapter = OfficialBrokerAdapter()
    assert adapter.reconcile() == {"healthy": False, "reason": "live adapter disabled"}
    with pytest.raises(RuntimeError, match="live broker execution is disabled"):
        adapter.submit_order(cast(Order, object()), Decimal("1"), 1)
