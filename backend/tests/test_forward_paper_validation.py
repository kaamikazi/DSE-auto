from __future__ import annotations

import csv
import hashlib
import io
import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, cast
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

import app.services.forward_paper_validation as forward_module
from app.core.config import Settings
from app.minimal_v1_cli import build_parser
from app.models import (
    AuditEvent,
    Order,
    PaperAccount,
    PaperSession,
    PaperSessionRun,
    RiskState,
    StrategyRegistration,
    Transaction,
)
from app.schemas.market import HistoricalBar, TimestampProvenance
from app.services.absolute_momentum_filter import deterministic_registration_id
from app.services.expanded_strategy_validation import EXPANDED_UNIVERSE, LoadedUniverse
from app.services.forward_paper_validation import (
    ACCOUNT_LABEL,
    FORWARD_INGEST_BOUNDARY_AT,
    FORWARD_INGEST_BOUNDARY_COMMIT,
    MANUAL_ATTESTATION,
    MANUAL_EVIDENCE_CLASS,
    MANUAL_SOURCE_IDENTITY,
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


def _operational_runner(
    db: Session,
    tmp_path: Path,
    *,
    implementation_boundary: datetime | None = None,
) -> ForwardPaperValidationRunner:
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
    return ForwardPaperValidationRunner(
        db,
        repository_root=tmp_path,
        settings=_settings(),
        implementation_boundary=implementation_boundary,
    )


def _decision() -> dict[str, Any]:
    return {
        "event_id": "decision-event",
        "decision_market_date": "2026-03-31",
        "data_snapshot_identity": "a" * 64,
        "execution_session": "2026-04-01",
        "selected": ["GP"],
        "target_weights": {"GP": 0.2},
    }


def _manual_csv(
    market_date: date,
    *,
    missing: set[str] | None = None,
    duplicate: str | None = None,
    invalid_ohlc: str | None = None,
    corrected_close: Decimal | None = None,
    zero_price: set[str] | None = None,
) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(
        [
            "#",
            "DATE",
            "TRADING CODE",
            "LTP*",
            "HIGH",
            "LOW",
            "OPENP*",
            "CLOSEP*",
            "YCP",
            "TRADE",
            "VALUE (mn)",
            "VOLUME",
        ]
    )
    present = [symbol for symbol in EXPANDED_UNIVERSE if symbol not in (missing or set())]
    for index, symbol in enumerate(present, 1):
        high, low, open_price, close = "101", "99", "100", "100"
        if symbol == invalid_ohlc:
            high, low = "98", "99"
        if symbol in (zero_price or set()):
            high = low = open_price = close = "0"
        if symbol == "GP" and corrected_close is not None:
            close = str(corrected_close)
        writer.writerow(
            [
                index,
                market_date.isoformat(),
                symbol,
                close,
                high,
                low,
                open_price,
                close,
                "100",
                "10",
                "1.0",
                "100000",
            ]
        )
    if duplicate is not None:
        writer.writerow(
            [
                len(present) + 1,
                market_date.isoformat(),
                duplicate,
                "100",
                "101",
                "99",
                "100",
                "100",
                "100",
                "10",
                "1.0",
                "100000",
            ]
        )
    return output.getvalue().encode()


def _manual_html_table(
    raw_csv: bytes, *, include_data: bool = True, aria_header: bool = False
) -> str:
    rows = list(csv.reader(io.StringIO(raw_csv.decode())))
    header, data = rows[0], rows[1:]
    parts = ["<table><thead><tr>"]
    parts.extend(
        f'<th aria-label="{cell}"></th>' if aria_header else f"<th>{cell}</th>" for cell in header
    )
    parts.append("</tr></thead><tbody>")
    for row in data if include_data else []:
        parts.append("<tr>")
        parts.extend(f"<td>{cell}</td>" for cell in row)
        parts.append("</tr>")
    parts.append("</tbody></table>")
    return "".join(parts)


def _manual_html(raw_csv: bytes) -> bytes:
    parts = ["<html><body>", _manual_html_table(raw_csv), "</body></html>"]
    return "".join(parts).encode()


def _multi_table_html(*tables: str) -> bytes:
    return ("<html><body>" + "".join(tables) + "</body></html>").encode()


def _manual_runner(
    db: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    settings: Settings | None = None,
) -> ForwardPaperValidationRunner:
    boundary = datetime(2026, 8, 9, 0, 0, tzinfo=UTC)
    runner = _operational_runner(db, tmp_path, implementation_boundary=boundary)
    if settings is not None:
        runner.settings = settings
    identity = {"registration_id": "registration"}
    loaded = LoadedUniverse({}, {}, [], {})
    monkeypatch.setattr(runner, "verify_startup", lambda: ({"strategy": identity}, loaded))
    return runner


def _ingest(
    runner: ForwardPaperValidationRunner,
    path: Path,
    *,
    market_date: date = date(2026, 8, 10),
    receipt: datetime = datetime(2026, 8, 10, 8, 15, tzinfo=UTC),
) -> dict[str, Any]:
    completed = datetime(2026, 8, 10, 14, 10, tzinfo=ZoneInfo("Asia/Dhaka"))
    return runner.ingest_manual_eod(
        path,
        market_date,
        MANUAL_SOURCE_IDENTITY,
        MANUAL_ATTESTATION,
        completed,
        receipt_time=receipt,
    )


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


def test_manual_ingest_enforces_local_file_source_and_attestation(
    db: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = _manual_runner(db, tmp_path, monkeypatch)
    source = tmp_path / "dse-eod.csv"
    source.write_bytes(_manual_csv(date(2026, 8, 10)))
    completed = datetime(2026, 8, 10, 14, 10, tzinfo=ZoneInfo("Asia/Dhaka"))
    with pytest.raises(ForwardValidationError, match="source identity"):
        runner.ingest_manual_eod(
            source,
            date(2026, 8, 10),
            "amarstock",
            MANUAL_ATTESTATION,
            completed,
        )
    with pytest.raises(ForwardValidationError, match="attest exactly"):
        runner.ingest_manual_eod(
            source,
            date(2026, 8, 10),
            MANUAL_SOURCE_IDENTITY,
            "I downloaded something",
            completed,
        )
    with pytest.raises(ForwardValidationError, match="only a local file"):
        runner.ingest_manual_eod(
            Path("https://www.dsebd.org/day_end_archive.php"),
            date(2026, 8, 10),
            MANUAL_SOURCE_IDENTITY,
            MANUAL_ATTESTATION,
            completed,
        )


def test_forward_ingest_cli_contract() -> None:
    parsed = build_parser().parse_args(
        [
            "forward-ingest",
            "--file",
            "dse-eod.csv",
            "--market-date",
            "2026-08-10",
            "--source",
            MANUAL_SOURCE_IDENTITY,
            "--session-completed-at",
            "2026-08-10T14:10:00+06:00",
            "--attestation",
            MANUAL_ATTESTATION,
        ]
    )
    assert parsed.command == "forward-ingest"
    assert parsed.source == MANUAL_SOURCE_IDENTITY
    assert parsed.attestation == MANUAL_ATTESTATION


@pytest.mark.parametrize("suffix", [".csv", ".html"])
def test_manual_ingest_preserves_hashes_and_normalizes_all_25_symbols(
    db: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    suffix: str,
) -> None:
    runner = _manual_runner(db, tmp_path, monkeypatch)
    csv_bytes = _manual_csv(date(2026, 8, 10))
    raw = csv_bytes if suffix == ".csv" else _manual_html(csv_bytes)
    source = tmp_path / f"dse-eod{suffix}"
    source.write_bytes(raw)
    first = _ingest(runner, source)
    second = _ingest(runner, source)
    assert first["event_id"] == second["event_id"]
    assert first["raw_sha256"] == hashlib.sha256(raw).hexdigest()
    assert first["evidence_class"] == MANUAL_EVIDENCE_CLASS
    assert first["adjustment_grain"] == "raw_unadjusted"
    assert first["eligible_symbols"] == list(EXPANDED_UNIVERSE)
    assert first["missing_symbols"] == []
    assert first["operator_attestation"] == MANUAL_ATTESTATION
    assert first["timestamp_semantics"] == "local receipt; not DSE publication time"
    raw_path = tmp_path / first["raw_snapshot_relative_path"]
    normalized_path = tmp_path / first["normalized_relative_path"]
    assert raw_path.read_bytes() == raw
    assert hashlib.sha256(normalized_path.read_bytes()).hexdigest() == first["normalized_sha256"]
    normalized = json.loads(normalized_path.read_bytes())
    assert len(normalized["observations"]) == 25
    assert normalized["adjustment_grain"] == "raw_unadjusted"
    session = runner._session()
    assert session is not None
    assert len(runner._manual_runs(session)) == 1
    audits = list(
        db.scalars(
            select(AuditEvent).where(
                AuditEvent.event_type == "data_import.activated",
                AuditEvent.entity_id == first["event_id"],
            )
        )
    )
    assert len(audits) == 1
    assert audits[0].event_metadata["operator_attestation"] == MANUAL_ATTESTATION


@pytest.mark.parametrize("header_only_first", [True, False])
def test_manual_ingest_selects_only_populated_matching_html_table(
    db: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    header_only_first: bool,
) -> None:
    runner = _manual_runner(db, tmp_path, monkeypatch)
    csv_bytes = _manual_csv(date(2026, 8, 10))
    empty = _manual_html_table(csv_bytes, include_data=False)
    populated = _manual_html_table(csv_bytes, aria_header=True)
    matching = [empty, populated] if header_only_first else [populated, empty]
    unrelated = "<table><tr><th>NOT DSE</th></tr><tr><td>ignored</td></tr></table>"
    raw = _multi_table_html(unrelated, *matching, unrelated)
    source = tmp_path / "saved-dse-page.html"
    source.write_bytes(raw)

    first = _ingest(runner, source)
    second = _ingest(runner, source)

    assert first["event_id"] == second["event_id"]
    assert first["parser_version"] == "dse_public_eod_manual_v2"
    assert first["source_row_count"] == len(EXPANDED_UNIVERSE)
    assert first["raw_sha256"] == hashlib.sha256(raw).hexdigest()
    assert source.read_bytes() == raw
    assert (tmp_path / first["raw_snapshot_relative_path"]).read_bytes() == raw
    session = runner._session()
    assert session is not None
    assert len(runner._manual_runs(session)) == 1


def test_manual_ingest_rejects_empty_and_ambiguous_matching_html_tables_without_effects(
    db: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = _manual_runner(db, tmp_path, monkeypatch)
    csv_bytes = _manual_csv(date(2026, 8, 10))
    empty = _manual_html_table(csv_bytes, include_data=False)
    counts_before = {
        model: int(db.scalar(select(func.count()).select_from(model)) or 0)
        for model in (PaperSessionRun, AuditEvent, Order, Transaction)
    }
    empty_source = tmp_path / "empty-tables.html"
    empty_source.write_bytes(_multi_table_html(empty, empty))
    with pytest.raises(ForwardValidationError, match="contains no observations"):
        _ingest(runner, empty_source)

    changed = _manual_csv(date(2026, 8, 10), corrected_close=Decimal("100.5"))
    ambiguous_source = tmp_path / "ambiguous-tables.html"
    ambiguous_source.write_bytes(
        _multi_table_html(_manual_html_table(csv_bytes), _manual_html_table(changed))
    )
    with pytest.raises(ForwardValidationError, match="ambiguous populated matching tables"):
        _ingest(runner, ambiguous_source)

    assert {
        model: int(db.scalar(select(func.count()).select_from(model)) or 0)
        for model in counts_before
    } == counts_before
    assert not (tmp_path / "data/process-state/minimal_v1_forward").exists()


def test_manual_html_keeps_date_and_duplicate_validation(
    db: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = _manual_runner(db, tmp_path, monkeypatch)
    mismatch = tmp_path / "mismatch.html"
    mismatch.write_bytes(_manual_html(_manual_csv(date(2026, 8, 10))))
    with pytest.raises(ForwardValidationError, match="Claimed-date mismatch"):
        runner.ingest_manual_eod(
            mismatch,
            date(2026, 8, 11),
            MANUAL_SOURCE_IDENTITY,
            MANUAL_ATTESTATION,
            datetime(2026, 8, 11, 14, 10, tzinfo=ZoneInfo("Asia/Dhaka")),
            receipt_time=datetime(2026, 8, 11, 8, 15, tzinfo=UTC),
        )

    duplicate = tmp_path / "duplicate.html"
    duplicate.write_bytes(_manual_html(_manual_csv(date(2026, 8, 10), duplicate="GP")))
    with pytest.raises(ForwardValidationError, match="Duplicate DSE symbol"):
        _ingest(runner, duplicate)


def test_committed_forward_boundary_remains_pinned_after_parser_fix(
    db: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = ForwardPaperValidationRunner(db, repository_root=tmp_path, settings=_settings())
    later_head = "f" * 40

    def fake_git(*arguments: str) -> str:
        if arguments == ("status", "--porcelain", "--untracked-files=no"):
            return ""
        if arguments == ("rev-parse", "HEAD"):
            return later_head
        if arguments[:1] == ("show",) and arguments[1].startswith(f"{later_head}:"):
            return "forward-ingest\ndef ingest_manual_eod"
        if arguments == (
            "merge-base",
            "--is-ancestor",
            FORWARD_INGEST_BOUNDARY_COMMIT,
            later_head,
        ):
            return ""
        if arguments == (
            "show",
            "-s",
            "--format=%cI",
            FORWARD_INGEST_BOUNDARY_COMMIT,
        ):
            return FORWARD_INGEST_BOUNDARY_AT.isoformat()
        raise AssertionError(arguments)

    monkeypatch.setattr(runner, "_git", fake_git)
    assert runner._implementation_boundary() == {
        "commit": FORWARD_INGEST_BOUNDARY_COMMIT,
        "committed_at": "2026-08-10T06:16:02+00:00",
    }


def test_manual_ingest_rejects_duplicate_invalid_ohlc_and_claimed_date_mismatch(
    db: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = _manual_runner(db, tmp_path, monkeypatch)
    duplicate = tmp_path / "duplicate.csv"
    duplicate.write_bytes(_manual_csv(date(2026, 8, 10), duplicate="GP"))
    with pytest.raises(ForwardValidationError, match="Duplicate DSE symbol"):
        _ingest(runner, duplicate)
    invalid = tmp_path / "invalid.csv"
    invalid.write_bytes(_manual_csv(date(2026, 8, 10), invalid_ohlc="GP"))
    with pytest.raises(ForwardValidationError, match="Malformed OHLC"):
        _ingest(runner, invalid)
    mismatch = tmp_path / "mismatch.csv"
    mismatch.write_bytes(_manual_csv(date(2026, 8, 10)))
    with pytest.raises(ForwardValidationError, match="Claimed-date mismatch"):
        runner.ingest_manual_eod(
            mismatch,
            date(2026, 8, 11),
            MANUAL_SOURCE_IDENTITY,
            MANUAL_ATTESTATION,
            datetime(2026, 8, 11, 14, 10, tzinfo=ZoneInfo("Asia/Dhaka")),
            receipt_time=datetime(2026, 8, 11, 8, 15, tzinfo=UTC),
        )


def test_manual_ingest_enforces_boundary_and_no_pre_receipt_visibility(
    db: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = _manual_runner(db, tmp_path, monkeypatch)
    backdated = tmp_path / "backdated.csv"
    backdated.write_bytes(_manual_csv(date(2026, 8, 8)))
    with pytest.raises(ForwardValidationError, match="committed implementation boundary"):
        runner.ingest_manual_eod(
            backdated,
            date(2026, 8, 8),
            MANUAL_SOURCE_IDENTITY,
            MANUAL_ATTESTATION,
            datetime(2026, 8, 8, 14, 10, tzinfo=ZoneInfo("Asia/Dhaka")),
            receipt_time=datetime(2026, 8, 10, 8, 15, tzinfo=UTC),
        )
    current = tmp_path / "current.csv"
    current.write_bytes(_manual_csv(date(2026, 8, 10)))
    result = _ingest(runner, current)
    session = runner._session()
    assert session is not None
    ingest = runner._manual_runs(session)[0]
    availability = datetime.fromisoformat(result["availability_timestamp"])
    assert availability == datetime.fromisoformat(result["receipt_timestamp"])
    with pytest.raises(ForwardValidationError, match="unavailable before local receipt"):
        runner._load_manual_observation(ingest, now=availability - timedelta(microseconds=1))


def test_manual_correction_creates_version_without_overwrite(
    db: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = _manual_runner(db, tmp_path, monkeypatch)
    original = tmp_path / "original.csv"
    original_bytes = _manual_csv(date(2026, 8, 10))
    original.write_bytes(original_bytes)
    first = _ingest(runner, original)
    first_path = tmp_path / first["raw_snapshot_relative_path"]
    corrected = tmp_path / "corrected.csv"
    corrected.write_bytes(_manual_csv(date(2026, 8, 10), corrected_close=Decimal("100.5")))
    second = _ingest(
        runner,
        corrected,
        receipt=datetime(2026, 8, 10, 8, 20, tzinfo=UTC),
    )
    assert second["version"] == 2
    assert second["supersedes_event_id"] == first["event_id"]
    assert second["raw_sha256"] != first["raw_sha256"]
    assert first_path.read_bytes() == original_bytes
    assert (tmp_path / second["raw_snapshot_relative_path"]).is_file()


def test_manual_raw_snapshot_tampering_fails_closed(
    db: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = _manual_runner(db, tmp_path, monkeypatch)
    source = tmp_path / "current.csv"
    source.write_bytes(_manual_csv(date(2026, 8, 10)))
    ingested = _ingest(runner, source)
    raw_path = tmp_path / ingested["raw_snapshot_relative_path"]
    raw_path.write_bytes(raw_path.read_bytes() + b"tampered")
    session = runner._session()
    assert session is not None
    with pytest.raises(ForwardValidationError, match="raw snapshot hash mismatch"):
        runner._load_manual_observation(
            runner._manual_runs(session)[0],
            now=datetime.fromisoformat(ingested["receipt_timestamp"]),
        )


def test_missing_symbol_is_preserved_and_runner_consumes_without_trading(
    db: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = _manual_runner(db, tmp_path, monkeypatch)
    source = tmp_path / "missing.csv"
    source.write_bytes(_manual_csv(date(2026, 8, 10), missing={"GP"}, zero_price={"IDLC"}))
    ingested = _ingest(runner, source)
    assert ingested["status"] == "accepted_with_missing"
    assert ingested["missing_symbols"] == ["GP", "IDLC"]
    assert ingested["unavailable_symbols"] == {
        "GP": "source_row_absent",
        "IDLC": "nonpositive_source_price_not_synthesized",
    }
    session = runner._session()
    assert session is not None
    receipt = datetime.fromisoformat(ingested["receipt_timestamp"])
    loaded = runner._load_manual_observation(runner._manual_runs(session)[0], now=receipt)
    bars = cast(dict[str, HistoricalBar], loaded["observations"])
    assert "GP" not in bars and "IDLC" not in bars
    assert all("raw_unadjusted" in bar.quality_flags for bar in bars.values())
    assert all("adjusted_execution" not in bar.quality_flags for bar in bars.values())
    consumed = runner._consume_manual_ingests(session, now=receipt)
    assert len(consumed) == 1
    assert consumed[0]["evidence_class"] == MANUAL_EVIDENCE_CLASS
    assert consumed[0]["mode"] == "forward"
    assert consumed[0]["decision_eligible"] is False
    assert "required_symbols_missing_without_substitution" in consumed[0]["decision_blockers"]
    assert len(runner._runs(session, "decision")) == 0
    assert int(db.scalar(select(func.count()).select_from(Order)) or 0) == 0
    assert int(db.scalar(select(func.count()).select_from(Transaction)) or 0) == 0


def test_unresolved_adjustment_blocks_decision_and_restart_is_deterministic(
    db: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = _manual_runner(db, tmp_path, monkeypatch)
    source = tmp_path / "current.csv"
    source.write_bytes(_manual_csv(date(2026, 8, 10)))
    first = _ingest(runner, source)
    session = runner._session()
    assert session is not None
    receipt = datetime.fromisoformat(first["receipt_timestamp"])
    consumed = runner._consume_manual_ingests(session, now=receipt)
    assert consumed[0]["analytical_adjustment_status"] == "unresolved"
    assert "analytical_adjustment_view_unresolved" in consumed[0]["decision_blockers"]
    assert runner._consume_manual_ingests(session, now=receipt) == []
    restarted = ForwardPaperValidationRunner(
        db,
        repository_root=tmp_path,
        settings=_settings(),
        implementation_boundary=datetime(2026, 8, 9, 0, 0, tzinfo=UTC),
    )
    identity = {"registration_id": "registration"}
    monkeypatch.setattr(
        restarted,
        "verify_startup",
        lambda: ({"strategy": identity}, LoadedUniverse({}, {}, [], {})),
    )
    repeated = _ingest(restarted, source)
    assert repeated["event_id"] == first["event_id"]
    assert len(restarted._manual_runs(session)) == 1
    assert restarted.manual_ingestion_status(session)["decision_eligible"] is False


def test_manual_ingest_preserves_paper_only_startup_boundary(
    db: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = _manual_runner(
        db,
        tmp_path,
        monkeypatch,
        settings=_settings(BROKER_ADAPTER="paper"),
    )
    source = tmp_path / "current.csv"
    source.write_bytes(_manual_csv(date(2026, 8, 10)))
    with pytest.raises(RuntimeError, match="Paper-only safety mismatch"):
        _ingest(runner, source)
