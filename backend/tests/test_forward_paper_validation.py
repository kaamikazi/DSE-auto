from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

import app.services.forward_paper_validation as forward_module
from app.core.config import Settings
from app.models import (
    Order,
    PaperAccount,
    PaperSession,
    RiskState,
    StrategyRegistration,
    Transaction,
)
from app.schemas.market import HistoricalBar, TimestampProvenance
from app.services.absolute_momentum_filter import deterministic_registration_id
from app.services.expanded_strategy_validation import EXPANDED_UNIVERSE, LoadedUniverse
from app.services.forward_paper_validation import (
    ACCOUNT_LABEL,
    SESSION_NAME,
    ForwardPaperValidationRunner,
    ForwardValidationError,
    RunnerLock,
    quarter_end_sessions,
)


def _settings(**updates: Any) -> Settings:
    values: dict[str, Any] = {
        "TRADING_MODE": "paper",
        "LIVE_TRADING_ENABLED": False,
        "BROKER_ADAPTER": "disabled",
        "DATABASE_ROLE": "test",
        "DATA_PRIMARY_PROVIDER": "mock",
        "DATA_MAX_STALENESS_SECONDS": 30,
    }
    values.update(updates)
    return Settings.model_construct(**values)


def _bar(
    symbol: str,
    day: date,
    *,
    close: Decimal = Decimal("100"),
    trusted: bool = False,
) -> HistoricalBar:
    return HistoricalBar(
        timestamp=datetime.combine(day, datetime.min.time(), UTC),
        symbol=symbol,
        open=Decimal("100"),
        high=max(Decimal("101"), close),
        low=min(Decimal("99"), close),
        close=close,
        volume=100_000,
        source="fixture",
        timestamp_provenance=(
            TimestampProvenance.OPERATOR_ATTESTED if trusted else TimestampProvenance.UNKNOWN
        ),
        quality_flags=["adjusted_execution", "lineage_validated"],
    )


def _operational_runner(db: Session, tmp_path: Path) -> ForwardPaperValidationRunner:
    db.add(PaperAccount(id=2, cash=Decimal("1000000"), starting_cash=Decimal("1000000")))
    db.add(
        PaperSession(
            id="forward-session",
            name=SESSION_NAME,
            account_id=2,
            state="running",
            starting_cash=Decimal("1000000"),
            approved_universe=sorted(EXPANDED_UNIVERSE),
            strategies=["absolute_momentum_filter@0.1.0"],
            risk_profile={
                "account_label": ACCOUNT_LABEL,
                "frozen_identity": {"registration_id": "registration"},
            },
            fill_model="pessimistic",
        )
    )
    db.commit()
    return ForwardPaperValidationRunner(db, repository_root=tmp_path, settings=_settings())


def _decision() -> dict[str, Any]:
    return {
        "event_id": "decision-event",
        "decision_market_date": "2026-03-31",
        "data_snapshot_identity": "a" * 64,
        "execution_session": "2026-04-01",
        "selected": ["GP"],
        "target_weights": {"GP": 0.2},
    }


def test_frozen_identity_checks_registration_and_dataset_contract(
    db: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    datasets = [
        {
            "registry_id": "dataset",
            "dataset_sha256": "d" * 64,
            "source_sha256": "s" * 64,
            "symbols": list(EXPANDED_UNIVERSE),
        }
    ]
    registration_id = deterministic_registration_id(
        code_sha256="c" * 64,
        parameter_sha256="p" * 64,
        datasets=[{"id": "dataset", "sha256": "d" * 64}],
    )
    identity = {
        "identity": "absolute_momentum_filter@0.1.0",
        "registration_id": registration_id,
        "code_sha256": "c" * 64,
        "parameter_sha256": "p" * 64,
    }
    expanded_datasets = [
        *datasets,
        *[
            {
                "registry_id": f"expanded-{index}",
                "dataset_sha256": str(index) * 64,
                "source_sha256": str(index + 1) * 64,
                "symbols": [],
            }
            for index in range(1, 4)
        ],
    ]
    db.add(
        StrategyRegistration(
            id=registration_id,
            strategy_id="absolute_momentum_filter",
            version="0.1.0",
            lifecycle_state="research",
            code_hash="c" * 64,
            parameters={},
            data_requirements={
                "active_dataset_ids_and_hashes": [{"id": "dataset", "sha256": "d" * 64}]
            },
            evidence={},
            minimum_sample_size=1,
        )
    )
    db.commit()
    loaded = LoadedUniverse({}, {}, expanded_datasets, {})
    monkeypatch.setattr(forward_module, "validate_frozen_identities", lambda *_: [identity])
    monkeypatch.setattr(forward_module, "load_expanded_universe", lambda *_: loaded)
    runner = ForwardPaperValidationRunner(db, repository_root=tmp_path, settings=_settings())
    verified, _ = runner.verify_frozen_contract()
    assert verified["registration_id"] == registration_id
    assert verified["timing_contract"]["same_bar_execution"] is False
    drifted = {**identity, "registration_id": "wrong"}
    monkeypatch.setattr(forward_module, "validate_frozen_identities", lambda *_: [drifted])
    with pytest.raises(ForwardValidationError, match="registration ID mismatch"):
        runner.verify_frozen_contract()


def test_paper_only_startup_and_replay_database_boundaries(db: Session, tmp_path: Path) -> None:
    unsafe = ForwardPaperValidationRunner(
        db,
        repository_root=tmp_path,
        settings=_settings(BROKER_ADAPTER="paper"),
    )
    with pytest.raises(RuntimeError, match="Paper-only safety mismatch"):
        unsafe.verify_startup()
    operational = ForwardPaperValidationRunner(
        db,
        repository_root=tmp_path,
        settings=_settings(DATABASE_ROLE="operational"),
    )
    with pytest.raises(ForwardValidationError, match="isolated test-role"):
        operational.run_replay(date(2025, 1, 1), date(2025, 1, 2))


def test_single_instance_lock_reports_owner(tmp_path: Path) -> None:
    path = tmp_path / "runner.lock"
    first = RunnerLock(path, {"runner": "first"})
    second = RunnerLock(path, {"runner": "second"})
    first.acquire()
    try:
        with pytest.raises(ForwardValidationError, match="already active"):
            second.acquire()
        assert first.existing_owner()["runner"] == "first"
    finally:
        first.release()


def test_rebalance_schedule_and_snapshot_ignore_future_values(db: Session, tmp_path: Path) -> None:
    sessions = [
        date(2026, 3, 30),
        date(2026, 3, 31),
        date(2026, 4, 1),
        date(2026, 6, 30),
    ]
    assert quarter_end_sessions(sessions) == {date(2026, 3, 31), date(2026, 6, 30)}
    runner = ForwardPaperValidationRunner(db, repository_root=tmp_path, settings=_settings())
    signal_day = date(2026, 3, 31)
    visible = {
        symbol: [_bar(symbol, signal_day), _bar(symbol, date(2026, 4, 1), close=Decimal("120"))]
        for symbol in EXPANDED_UNIVERSE
    }
    first = runner._snapshot_identity(signal_day, visible, [])
    changed = {
        symbol: [rows[0], _bar(symbol, date(2026, 4, 1), close=Decimal("500"))]
        for symbol, rows in visible.items()
    }
    assert runner._snapshot_identity(signal_day, changed, []) == first


def test_next_open_cost_cash_and_idempotent_crash_recovery(db: Session, tmp_path: Path) -> None:
    runner = _operational_runner(db, tmp_path)
    day = date(2026, 4, 1)
    observations = {symbol: _bar(symbol, day) for symbol in EXPANDED_UNIVERSE}
    session = runner._session()
    assert session is not None
    first = runner._execution(session, _decision(), day, observations, replay=True)
    assert first["execution_session"] == "2026-04-01"
    assert first["fills"][0]["fill_price"] == "100.25"
    assert Decimal(first["fills"][0]["fee"]) > 0
    account = db.get(PaperAccount, 2)
    assert account is not None and Decimal("0") <= account.cash < Decimal("1000000")
    before = {
        "orders": int(db.scalar(select(func.count()).select_from(Order)) or 0),
        "transactions": int(db.scalar(select(func.count()).select_from(Transaction)) or 0),
    }
    second = runner._execution(session, _decision(), day, observations, replay=True)
    assert second["event_id"] == first["event_id"]
    after = {
        "orders": int(db.scalar(select(func.count()).select_from(Order)) or 0),
        "transactions": int(db.scalar(select(func.count()).select_from(Transaction)) or 0),
    }
    assert after == before == {"orders": 1, "transactions": 1}
    execution = next(
        item
        for item in runner._runs(session, "execution")
        if item.metrics["event_id"] == first["event_id"]
    )
    db.delete(execution)
    db.commit()
    recovered = runner._execution(session, _decision(), day, observations, replay=True)
    assert recovered["holdings"]["GP"] == first["holdings"]["GP"]
    assert int(db.scalar(select(func.count()).select_from(Order)) or 0) == 1
    assert int(db.scalar(select(func.count()).select_from(Transaction)) or 0) == 1
    assert runner.reconcile(market_date=day, event_class="replay")["healthy"] is True
    recovered_run = next(
        item
        for item in runner._runs(session, "execution")
        if item.metrics["event_id"] == recovered["event_id"]
    )
    db.delete(recovered_run)
    db.commit()
    changed = {**observations, "GP": _bar("GP", day, close=Decimal("110"))}
    with pytest.raises(ForwardValidationError, match="Execution data changed"):
        runner._execution(session, _decision(), day, changed, replay=True)


def test_missing_stale_and_emergency_halts_without_liquidation(db: Session, tmp_path: Path) -> None:
    runner = _operational_runner(db, tmp_path)
    day = date(2026, 4, 1)
    observations = {symbol: _bar(symbol, day) for symbol in EXPANDED_UNIVERSE}
    observations.pop("GP")
    with pytest.raises(ForwardValidationError, match="missing=.*GP"):
        runner._validate_observation(day, observations, replay=True)
    trusted = {symbol: _bar(symbol, day, trusted=True) for symbol in EXPANDED_UNIVERSE}
    with pytest.raises(ForwardValidationError, match="Stale market data"):
        runner._validate_observation(
            day,
            trusted,
            replay=False,
            now=datetime.combine(day + timedelta(days=1), datetime.min.time(), UTC),
        )
    session = runner._session()
    assert session is not None
    transaction_count = int(db.scalar(select(func.count()).select_from(Transaction)) or 0)
    status = runner.emergency_halt("operator test halt")
    assert status["runtime_state"] == "HALTED"
    assert runner.portfolio()["holdings"] == {symbol: 0 for symbol in EXPANDED_UNIVERSE}
    assert int(db.scalar(select(func.count()).select_from(Transaction)) or 0) == transaction_count
    with pytest.raises(ForwardValidationError, match="Emergency halt"):
        runner._execution(session, _decision(), day, trusted, replay=True)
    with pytest.raises(ForwardValidationError, match="explicit --resume-emergency"):
        runner._activate(session, mode="replay", resume_emergency=False)
    runner._activate(session, mode="replay", resume_emergency=True)
    assert runner.status()["runtime_state"] == "DEGRADED"
    db.add(RiskState(id=1, state="emergency_stop", reason="global test halt"))
    db.commit()
    with pytest.raises(ForwardValidationError, match="Global emergency"):
        runner._execution(session, _decision(), day, trusted, replay=True)
    assert runner.status()["runtime_state"] == "HALTED"


def test_execution_plan_is_durable_before_order_effect(db: Session, tmp_path: Path) -> None:
    runner = _operational_runner(db, tmp_path)
    session = runner._session()
    assert session is not None
    day = date(2026, 4, 1)
    observations = {symbol: _bar(symbol, day) for symbol in EXPANDED_UNIVERSE}
    runner._execution(session, _decision(), day, observations, replay=True)
    plans = runner._runs(session, "execution_plan")
    assert len(plans) == 1
    assert plans[0].metrics["target_quantities"]["GP"] > 0
    assert len(runner._runs(session, "execution")) == 1
