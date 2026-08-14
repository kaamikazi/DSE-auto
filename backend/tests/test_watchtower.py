from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from app.services.watchtower import (
    DataStatus,
    EventEvidence,
    Feature,
    FeatureStatus,
    InstrumentMetadata,
    VerificationStatus,
    attention_score,
    build_watchtower_report,
    load_day_end_sessions,
    median_and_mad,
    parse_day_end_file,
    run_watchtower,
    safe_multiple,
)

HEADER = (
    "DATE,TRADING CODE,LTP*,HIGH,LOW,OPENP*,CLOSEP*,YCP,TRADE,VALUE (mn),VOLUME\n"
)


def _row(
    day: date,
    code: str,
    *,
    close: str = "100",
    ycp: str = "100",
    open_price: str = "100",
    high: str = "101",
    low: str = "99",
    ltp: str = "100",
    trades: int = 10,
    value: str = "1",
    volume: int = 1000,
) -> str:
    return (
        f"{day.isoformat()},{code},{ltp},{high},{low},{open_price},{close},{ycp},"
        f"{trades},{value},{volume}\n"
    )


def _write_csv(directory: Path, day: date, rows: list[str]) -> Path:
    path = directory / f"{day.isoformat()}.csv"
    path.write_text(HEADER + "".join(rows), encoding="utf-8")
    return path


def _verified(code: str = "ABC") -> InstrumentMetadata:
    return InstrumentMetadata(
        trading_code=code,
        company_name=f"{code} Limited",
        sector="Engineering",
        instrument_type="EQUITY",
        market_category="A",
        listing_status="ACTIVE",
        observed_at="2026-08-13T15:00:00+06:00",
        source_reference="local-official-profile.html",
        verification_status=VerificationStatus.VERIFIED_EQUITY,
    )


def _history(directory: Path, count: int, *, current_anomaly: bool = False) -> None:
    first = date(2026, 1, 1)
    for offset in range(count):
        day = first + timedelta(days=offset)
        latest = offset == count - 1 and current_anomaly
        rows = [
            _row(
                day,
                "ABC",
                close="110" if latest else "100",
                ycp="100",
                open_price="105" if latest else "100",
                high="110" if latest else "101",
                low="104" if latest else "99",
                ltp="110" if latest else "100",
                trades=100 if latest else 10,
                value="10" if latest else "1",
                volume=10000 if latest else 1000,
            ),
            _row(
                day,
                "XYZ",
                close="110" if latest else "100",
                ycp="100",
                open_price="105" if latest else "100",
                high="110" if latest else "101",
                low="104" if latest else "99",
                ltp="110" if latest else "100",
                trades=100 if latest else 10,
                value="10" if latest else "1",
                volume=10000 if latest else 1000,
            ),
        ]
        _write_csv(directory, day, rows)


def test_dse_html_parsing_reuses_forward_table_extractor_and_preserves_zero_activity(
    tmp_path: Path,
) -> None:
    day = date(2026, 8, 13)
    populated = HEADER + _row(day, "ABC") + _row(
        day,
        "ZERO",
        close="0",
        ycp="50",
        open_price="0",
        high="0",
        low="0",
        ltp="0",
        trades=0,
        value="0",
        volume=0,
    )
    header_cells = "".join(f"<th>{cell}</th>" for cell in HEADER.strip().split(","))
    data_rows = "".join(
        "<tr>" + "".join(f"<td>{cell}</td>" for cell in row.split(",")) + "</tr>"
        for row in populated.strip().splitlines()[1:]
    )
    source = tmp_path / "day-end.html"
    source.write_text(
        f"<html><table><tr>{header_cells}</tr>{data_rows}</table></html>",
        encoding="utf-8",
    )

    session = parse_day_end_file(source)

    assert session.market_date == day
    assert len(session.observations) == 2
    by_code = {item.trading_code: item for item in session.observations}
    assert by_code["ABC"].data_status is DataStatus.USABLE
    assert by_code["ZERO"].data_status is DataStatus.ZERO_ACTIVITY
    assert by_code["ZERO"].close == 0
    assert by_code["ZERO"].unavailable_reason == "source_reports_zero_activity"


def test_verified_and_unverified_handling_blocks_unverified_watchlist_candidate(
    tmp_path: Path,
) -> None:
    _history(tmp_path, 41, current_anomaly=True)
    report = build_watchtower_report(load_day_end_sessions(tmp_path), {"ABC": _verified()}, ())
    records = {item["trading_code"]: item for item in report["records"]}

    assert records["ABC"]["report_label"] == "HIGH_ATTENTION"
    assert records["ABC"]["watchlist_candidate_eligible"] is True
    assert records["ABC"]["attention_score"]["total"] >= 8
    assert records["XYZ"]["instrument"]["verification_status"] == "UNVERIFIED_INSTRUMENT"
    assert records["XYZ"]["report_label"] == "DATA_ISSUE"
    assert records["XYZ"]["watchlist_candidate_eligible"] is False
    assert records["XYZ"]["attention_score"]["total"] >= 8


def test_insufficient_history_is_explicit_for_verified_equity(tmp_path: Path) -> None:
    _history(tmp_path, 3)
    report = build_watchtower_report(load_day_end_sessions(tmp_path), {"ABC": _verified()}, ())
    record = next(item for item in report["records"] if item["trading_code"] == "ABC")

    assert record["report_label"] == "INSUFFICIENT_HISTORY"
    assert record["watchlist_candidate_eligible"] is False
    assert record["features"]["daily_return_pct"]["status"] == "AVAILABLE"
    assert record["features"]["volume_multiple"]["status"] == "INSUFFICIENT_HISTORY"


def test_median_mad_and_zero_median_are_safe() -> None:
    median, mad = median_and_mad(
        [Decimal("1"), Decimal("2"), Decimal("2"), Decimal("3"), Decimal("100")]
    )
    assert median == Decimal("2")
    assert mad == Decimal("1")

    zero_baseline = safe_multiple(Decimal("5"), [Decimal(0)] * 40)
    assert zero_baseline.status is FeatureStatus.UNAVAILABLE
    assert zero_baseline.reason == "trailing_median_is_zero"


def test_attention_score_is_deterministic_and_explainable() -> None:
    unavailable = Feature(FeatureStatus.UNAVAILABLE, reason="test")
    features = {
        "daily_return_pct": Feature(FeatureStatus.AVAILABLE, Decimal("10"), "percent"),
        "opening_gap_pct": Feature(FeatureStatus.AVAILABLE, Decimal("5"), "percent"),
        "intraday_range_pct": Feature(FeatureStatus.AVAILABLE, Decimal("8"), "percent"),
        "volume_multiple": Feature(FeatureStatus.AVAILABLE, Decimal("10"), "multiple"),
        "trade_count_multiple": unavailable,
        "traded_value_multiple": unavailable,
        "robust_return_z": Feature(FeatureStatus.AVAILABLE, Decimal("5"), "robust_z"),
        "robust_volume_z": unavailable,
        "volatility_expansion": unavailable,
        "recent_high_low_breakout": Feature(
            FeatureStatus.AVAILABLE, "HIGH", "observation"
        ),
    }

    first = attention_score(features)
    second = attention_score(features)

    assert first == second
    assert first[0] == 14
    assert sum(first[1].values()) == first[0]


def test_event_evidence_never_proves_causality_or_changes_score_and_tier_e_cannot_escalate(
    tmp_path: Path,
) -> None:
    _history(tmp_path, 41)
    sessions = load_day_end_sessions(tmp_path)
    base = build_watchtower_report(sessions, {"ABC": _verified()}, ())
    event = EventEvidence(
        trading_code="ABC",
        event_type="rumour",
        event_time=datetime(2026, 2, 10, tzinfo=ZoneInfo("Asia/Dhaka")),
        publication_time=datetime(2026, 2, 10, tzinfo=ZoneInfo("Asia/Dhaka")),
        observed_at=datetime(2026, 2, 10, tzinfo=ZoneInfo("Asia/Dhaka")),
        source_tier="E",
        source_reference="manual-rumour-note",
        short_factual_summary="Unverified claim requiring investigation.",
        contradiction_flag=False,
    )
    with_event = build_watchtower_report(sessions, {"ABC": _verified()}, (event,))
    base_record = next(item for item in base["records"] if item["trading_code"] == "ABC")
    event_record = next(
        item for item in with_event["records"] if item["trading_code"] == "ABC"
    )

    assert event_record["attention_score"] == base_record["attention_score"]
    assert event_record["report_label"] == base_record["report_label"] == "NORMAL"
    assert event_record["event_evidence"]["status"] == "RUMOUR_ONLY_INVESTIGATE"
    assert event_record["event_evidence"]["causality_inferred"] is False


def test_run_is_deterministic_and_does_not_mutate_sources_or_database(tmp_path: Path) -> None:
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    day = date(2026, 8, 13)
    source = _write_csv(inputs, day, [_row(day, "ABC")])
    master = tmp_path / "master.csv"
    master.write_text(
        "trading_code,company_name,sector,instrument_type,market_category,listing_status,observed_at,source_reference,verification_status\n",
        encoding="utf-8",
    )
    events = tmp_path / "events.json"
    events.write_text("[]\n", encoding="utf-8")
    database = tmp_path / "operational.db"
    database.write_bytes(b"protected-operational-database")
    source_before = hashlib.sha256(source.read_bytes()).hexdigest()
    database_before = hashlib.sha256(database.read_bytes()).hexdigest()
    output = tmp_path / "reports"

    first = run_watchtower(
        day_end_directory=inputs,
        instrument_master_path=master,
        event_evidence_path=events,
        output_root=output,
        protected_database_path=database,
    )
    first_bytes = {
        name: Path(path).read_bytes() for name, path in first["artifacts"].items()
    }
    second = run_watchtower(
        day_end_directory=inputs,
        instrument_master_path=master,
        event_evidence_path=events,
        output_root=output,
        protected_database_path=database,
    )

    assert first == second
    assert first_bytes == {
        name: Path(path).read_bytes() for name, path in second["artifacts"].items()
    }
    assert hashlib.sha256(source.read_bytes()).hexdigest() == source_before
    assert hashlib.sha256(database.read_bytes()).hexdigest() == database_before
    payload = json.loads(Path(first["artifacts"]["json"]).read_text(encoding="utf-8"))
    labels = {record["report_label"] for record in payload["records"]}
    assert labels <= {"NORMAL", "WATCH", "HIGH_ATTENTION", "DATA_ISSUE", "INSUFFICIENT_HISTORY"}
    assert payload["safety"]["orders_created"] == 0
    assert payload["safety"]["fills_created"] == 0
    assert payload["safety"]["transactions_created"] == 0
    assert payload["safety"]["network_used"] is False
