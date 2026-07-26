from __future__ import annotations

import io
import zipfile
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    NormalizedDailyBar,
    Order,
    PortfolioStatementDraft,
    Transaction,
    UniverseMembershipPeriod,
    ValidationCampaign,
)
from app.services.governed_research_data import (
    activate_for_research,
    compare_sources,
    create_universe,
    eligible_on,
    portfolio_decision_support,
    preview_import,
    register_corporate_action,
    register_dataset,
    rollback_import,
    suspicious_discontinuities,
    workspace_summary,
)

CSV = b"symbol,date,open,high,low,close,adjusted_close,volume,value,trades,previous_close,publication_time,receipt_time\nGP,2026-07-20,100,105,99,103,103,1000,103000,50,101,2026-07-20T18:00:00+06:00,2026-07-20T18:02:00+06:00\nACI,2026-07-20,200,204,198,202,202,500,101000,25,199,2026-07-20T18:00:00+06:00,2026-07-20T18:02:00+06:00\n"
MAPPING = {
    "symbol": "symbol",
    "trading_date": "date",
    "open": "open",
    "high": "high",
    "low": "low",
    "close": "close",
    "adjusted_close": "adjusted_close",
    "volume": "volume",
    "value": "value",
    "number_of_trades": "trades",
    "previous_close": "previous_close",
    "source_publication_timestamp": "publication_time",
    "system_receipt_timestamp": "receipt_time",
}


def _dataset(db: Session, tmp_path: Path, raw: bytes = CSV, name: str = "fixture.csv"):  # type: ignore[no-untyped-def]
    return register_dataset(
        db,
        filename=name,
        raw=raw,
        raw_dir=tmp_path / "raw",
        source_category="manual_broker",
        source_name=f"deterministic-{name}",
        source_reference=f"fixture://{name}",
        publisher="Synthetic fixture publisher",
        license_note="Generated test fixture; no redistribution restriction",
        operator="test-operator",
        timestamp_trust="operator_attested",
        source_trust="operator_attested",
        stated_symbol_coverage=["GP", "ACI"],
        adjustment_status="adjusted",
    )


def _activate(db: Session, tmp_path: Path, raw: bytes = CSV, name: str = "fixture.csv"):  # type: ignore[no-untyped-def]
    dataset = _dataset(db, tmp_path, raw, name)
    run = preview_import(db, dataset, column_mapping=MAPPING)
    activate_for_research(db, run, operator="test-operator", normalized_dir=tmp_path / "normalized")
    return dataset, run


def test_safe_archive_extraction_and_traversal_rejection(db: Session, tmp_path: Path) -> None:
    safe = io.BytesIO()
    with zipfile.ZipFile(safe, "w") as archive:
        archive.writestr("daily.csv", CSV)
    dataset = _dataset(db, tmp_path, safe.getvalue(), "daily.zip")
    assert preview_import(db, dataset, column_mapping=MAPPING).row_count == 2

    unsafe = io.BytesIO()
    with zipfile.ZipFile(unsafe, "w") as archive:
        archive.writestr("../escape.csv", CSV)
    with pytest.raises(ValueError, match="path traversal"):
        _dataset(db, tmp_path, unsafe.getvalue(), "escape.zip")


def test_duplicate_file_batch_schema_mapping_and_timestamp_separation(
    db: Session, tmp_path: Path
) -> None:
    dataset = _dataset(db, tmp_path)
    with pytest.raises(ValueError, match="Duplicate dataset"):
        _dataset(db, tmp_path)
    run = preview_import(db, dataset, column_mapping=MAPPING)
    assert run.preview["timestamp_types_kept_separate"] is True
    assert (
        run.preview["sample"][0]["timestamp_provenance"]["source_publication_timestamp"]
        != run.preview["sample"][0]["timestamp_provenance"]["system_receipt_timestamp"]
    )
    with pytest.raises(ValueError, match="Duplicate import batch"):
        preview_import(db, dataset, column_mapping=MAPPING)


def test_ohlc_validation_blocks_activation(db: Session, tmp_path: Path) -> None:
    bad = CSV.replace(b"100,105,99,103", b"100,101,99,103")
    dataset = _dataset(db, tmp_path, bad)
    run = preview_import(db, dataset, column_mapping=MAPPING)
    assert run.state == "review_required"
    assert run.errors[0]["classification"] == "invalid_ohlc"
    with pytest.raises(ValueError, match="clean preview"):
        activate_for_research(db, run, operator="test", normalized_dir=tmp_path / "normalized")


def test_research_activation_lineage_and_rollback_are_isolated(db: Session, tmp_path: Path) -> None:
    dataset, run = _activate(db, tmp_path)
    bars = list(db.scalars(select(NormalizedDailyBar)))
    assert len(bars) == 2
    assert all(bar.dataset_id == dataset.id and bar.batch_hash == run.batch_hash for bar in bars)
    rollback_import(db, run, operator="test-operator")
    assert db.scalar(select(func.count()).select_from(NormalizedDailyBar)) == 0
    assert db.scalar(select(func.count()).select_from(Order)) == 0
    assert db.scalar(select(func.count()).select_from(Transaction)) == 0


def test_cross_source_conflict_is_not_averaged(db: Session, tmp_path: Path) -> None:
    first, _ = _activate(db, tmp_path / "one", CSV, "one.csv")
    second_csv = CSV.replace(b"100,105,99,103,103", b"100,140,99,135,135")
    second, _ = _activate(db, tmp_path / "two", second_csv, "two.csv")
    result = compare_sources(db, first.id, second.id, output_dir=tmp_path / "reports")
    assert result.report["prices_averaged"] is False
    assert result.report["counts"]["corporate_action_suspected"] == 1
    assert set(result.output_paths) == {"json", "csv", "html"}


def test_dsex_and_missing_dates_are_reported(db: Session, tmp_path: Path) -> None:
    raw = CSV.replace(b"GP,", b"DSEX,").replace(b"ACI,2026-07-20", b"DSEX,2026-07-22")
    dataset, _ = _activate(db, tmp_path, raw, "dsex.csv")
    bars = list(
        db.scalars(select(NormalizedDailyBar).where(NormalizedDailyBar.dataset_id == dataset.id))
    )
    assert {bar.symbol for bar in bars} == {"DSEX"}
    assert (bars[1].trading_date - bars[0].trading_date).days == 2


def test_corporate_action_inference_needs_review_and_discontinuity_is_flagged(
    db: Session, tmp_path: Path
) -> None:
    _, _ = _activate(db, tmp_path)
    bars = list(db.scalars(select(NormalizedDailyBar).where(NormalizedDailyBar.symbol == "GP")))
    duplicate = NormalizedDailyBar(
        **{
            column.name: getattr(bars[0], column.name)
            for column in NormalizedDailyBar.__table__.columns
            if column.name not in {"id", "trading_date", "source_row_id", "close", "high"}
        },
        trading_date=date(2026, 7, 21),
        source_row_id="root:99",
        close=Decimal("60"),
        high=Decimal("100"),
    )
    bars.append(duplicate)
    assert suspicious_discontinuities(bars)[0]["automatically_approved"] is False
    action = register_corporate_action(
        db, symbol="GP", event_type="stock_split", inferred=True, adjustment_factor=Decimal("0.5")
    )
    assert action.review_decision == "review_required"
    with pytest.raises(ValueError, match="cannot be automatically verified"):
        register_corporate_action(
            db, symbol="GP", event_type="stock_split", inferred=True, verification_status="verified"
        )


def test_survivorship_controlled_eligibility() -> None:
    membership = UniverseMembershipPeriod(
        universe_id="u",
        symbol="GP",
        eligible_from=date(2020, 1, 1),
        eligible_to=date(2020, 12, 31),
        suspension_periods=[{"from": "2020-06-01", "to": "2020-06-10"}],
    )
    assert eligible_on(membership, date(2020, 5, 1)) is True
    assert eligible_on(membership, date(2020, 6, 5)) is False
    assert eligible_on(membership, date(2021, 1, 1)) is False


def test_initial_universe_remains_draft(db: Session) -> None:
    universe = create_universe(
        db,
        name="initial-gp-aci-bracbank",
        memberships=[
            {"symbol": symbol, "eligible_from": date(2020, 1, 1)}
            for symbol in ("GP", "ACI", "BRACBANK")
        ],
    )
    assert universe.status == "draft"
    assert db.scalar(select(func.count()).select_from(UniverseMembershipPeriod)) == 3


def test_portfolio_support_is_read_only(db: Session) -> None:
    draft = PortfolioStatementDraft(
        evidence_id="evidence",
        broker_label="redacted-broker",
        account_label="read-only",
        statement_date=date(2026, 7, 20),
        statement_hash="a" * 64,
        parsed_data={
            "holdings": [{"symbol": "GP", "quantity": "10", "average_acquisition_cost": "90"}],
            "cash_balance": "100",
        },
        reconciliation_summary={},
        discrepancies=[],
        state="previewed",
    )
    report = portfolio_decision_support(draft, {"GP": Decimal("100")}, {"GP": "telecom"})
    assert report["banner"] == "REAL PORTFOLIO — READ ONLY"
    assert report["instructions"] is False
    assert report["orders_created"] == 0


def test_summary_proves_no_activation_campaign_order_or_fill(db: Session) -> None:
    summary = workspace_summary(db)
    assert summary["proof_no_activation"] == {
        "strategy_promotions": 0,
        "campaigns": 0,
        "orders": 0,
        "fills_or_transactions": 0,
    }
    assert db.scalar(select(func.count()).select_from(ValidationCampaign)) == 0
    assert summary["qualification"] == "0/60"


def test_research_data_api_exposes_read_only_operations(client) -> None:  # type: ignore[no-untyped-def]
    summary = client.get("/api/v1/research-data/summary")
    assert summary.status_code == 200
    assert "REAL PORTFOLIO READ ONLY" in summary.json()["banners"]
    workflow = client.get("/api/v1/research-data/workflow/eod").json()
    assert workflow["order_submission"] is False
    questions = client.get("/api/v1/research-data/questionnaires").json()
    assert questions["data_vendor"] and questions["broker"]
    assert client.post("/api/v1/research-data/compare", json={}).status_code == 401
