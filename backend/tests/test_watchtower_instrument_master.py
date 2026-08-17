from __future__ import annotations

import hashlib
import json
import socket
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.services.watchtower import (
    DataStatus,
    DayEndSession,
    MarketObservation,
    WatchtowerError,
    build_watchtower_report,
    load_instrument_master,
)
from app.services.watchtower_instrument_master import (
    build_local_instrument_master,
    parse_company_listing_html,
    parse_industry_listing_html,
)


def _company_html(entries: list[tuple[str, str]], *, declared_count: int) -> str:
    links = "".join(
        f'<a href="https://www.dsebd.org/displayCompany.php?name={code}" class="ab1 ">'
        f"{code}</a> <span>({name})<br></span>"
        for code, name in entries
    )
    return (
        "<html><head><title>Company Listing | Dhaka Stock Exchange</title></head><body>"
        '<a href="https://www.dsebd.org/displayCompany.php?name=TICKER" '
        'class="abhead">TICKER</a>'
        f'<h2 class="BodyHead topBodyHead">Total Company List:&nbsp; {declared_count}</h2>'
        f'<div class="BodyContent">{links}</div></body></html>'
    )


def _industry_html(entries: list[tuple[str, str, int]]) -> str:
    rows = "".join(
        "<tr>"
        f"<td>{index}</td>"
        f'<td><a href="https://www.dsebd.org/companylistbyindustry.php?industryno={number}" '
        f'class="ab1">{name}</a></td><td>{quantity}</td><td>More info</td></tr>'
        for index, (number, name, quantity) in enumerate(entries, 1)
    )
    return (
        "<html><head><title>Sector wise Company List | Dhaka Stock Exchange</title>"
        "</head><body><h2>Sector wise Company List</h2><table>"
        "<tr><th>#</th><th>Name of the Industry</th><th>Quantity</th><th>Detail</th></tr>"
        f"{rows}</table></body></html>"
    )


def _write_sources(
    directory: Path,
    *,
    company_entries: list[tuple[str, str]],
    declared_count: int,
    industries: list[tuple[str, str, int]],
) -> tuple[Path, Path]:
    directory.mkdir()
    company = directory / "opaque-a.html"
    company.write_text(
        _company_html(company_entries, declared_count=declared_count), encoding="utf-8"
    )
    industry = directory / "opaque-b.html"
    industry.write_text(_industry_html(industries), encoding="utf-8")
    return company, industry


def _observation(day: date, *, anomalous: bool) -> MarketObservation:
    return MarketObservation(
        market_date=day,
        trading_code="ABC",
        open=Decimal("105") if anomalous else Decimal("100"),
        high=Decimal("110") if anomalous else Decimal("101"),
        low=Decimal("104") if anomalous else Decimal("99"),
        close=Decimal("110") if anomalous else Decimal("100"),
        ltp=Decimal("110") if anomalous else Decimal("100"),
        ycp=Decimal("100"),
        volume=10_000 if anomalous else 1_000,
        trade_count=100 if anomalous else 10,
        traded_value_mn=Decimal("10") if anomalous else Decimal("1"),
        data_status=DataStatus.USABLE,
        unavailable_reason=None,
    )


def test_company_parser_extracts_only_main_exact_profile_links() -> None:
    parsed = parse_company_listing_html(
        _company_html(
            [("ABC", "ABC Limited"), ("AMCL(PRAN)", "Pran"), ("KAY&QUE", "Kay & Que")],
            declared_count=3,
        )
    )

    assert parsed.declared_count == 3
    assert [record.trading_code for record in parsed.records] == [
        "ABC",
        "AMCL(PRAN)",
        "KAY&QUE",
    ]
    assert parsed.records[0].company_name == "ABC Limited"
    assert parsed.records[0].profile_reference.endswith("displayCompany.php?name=ABC")
    assert "TICKER" not in {record.trading_code for record in parsed.records}


def test_industry_parser_extracts_summary_without_inventing_code_membership() -> None:
    parsed = parse_industry_listing_html(
        _industry_html([("13", "Engineering", 1), ("26", "Corporate Bond", 2)])
    )

    assert [(record.industry_name, record.quantity) for record in parsed.records] == [
        ("Engineering", 1),
        ("Corporate Bond", 2),
    ]
    assert parsed.records[0].detail_reference.endswith("industryno=13")


def test_exact_code_build_is_partial_deterministic_hashed_and_network_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    evidence = tmp_path / "evidence"
    company, industry = _write_sources(
        evidence,
        company_entries=[("ABC", "ABC Limited"), ("XYZ", "XYZ PLC")],
        declared_count=2,
        industries=[("13", "Engineering", 2)],
    )
    master = tmp_path / "config" / "master.csv"
    provenance = tmp_path / "config" / "master.provenance.json"
    source_hashes = {
        path: hashlib.sha256(path.read_bytes()).hexdigest() for path in (company, industry)
    }

    def forbid_network(*args: object, **kwargs: object) -> None:
        raise AssertionError("network access is forbidden")

    monkeypatch.setattr(socket, "socket", forbid_network)
    first = build_local_instrument_master(
        evidence_directory=evidence,
        instrument_master_path=master,
        provenance_path=provenance,
        repository_root=tmp_path,
    )
    first_master = master.read_bytes()
    first_provenance = provenance.read_bytes()
    second = build_local_instrument_master(
        evidence_directory=evidence,
        instrument_master_path=master,
        provenance_path=provenance,
        repository_root=tmp_path,
    )

    assert first == second
    assert master.read_bytes() == first_master
    assert provenance.read_bytes() == first_provenance
    assert first.exact_code_sector_joins == 0
    assert first.record_conflicts == 0
    assert all(hashlib.sha256(path.read_bytes()).hexdigest() == digest for path, digest in source_hashes.items())
    loaded = load_instrument_master(master)
    assert set(loaded) == {"ABC", "XYZ"}
    assert loaded["ABC"].company_name == "ABC Limited"
    assert loaded["ABC"].sector == ""
    assert loaded["ABC"].instrument_type == ""
    assert loaded["ABC"].market_category == ""
    assert loaded["ABC"].listing_status == ""
    assert loaded["ABC"].verification_status.value == "UNVERIFIED_INSTRUMENT"
    payload = json.loads(provenance.read_text(encoding="utf-8"))
    assert payload["policy"]["fuzzy_matching_used"] is False
    assert payload["policy"]["guessed_metadata_used"] is False
    assert payload["summary"]["exact_code_sector_joins"] == 0
    assert payload["records"]["ABC"]["resolution_status"] == "PARTIAL_METADATA"
    assert payload["records"]["ABC"]["evidence_source_ids"] == ["company_listing"]
    company_source = next(
        source for source in payload["sources"] if source["source_id"] == "company_listing"
    )
    assert company_source["sha256"] == source_hashes[company]
    assert payload["master"]["sha256"] == hashlib.sha256(master.read_bytes()).hexdigest()


def test_conflicting_exact_code_is_internal_nonactionable_conflict(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence"
    _write_sources(
        evidence,
        company_entries=[("ABC", "ABC Limited"), ("ABC", "Different Name")],
        declared_count=1,
        industries=[("13", "Engineering", 1)],
    )
    master = tmp_path / "master.csv"
    provenance = tmp_path / "master.provenance.json"

    result = build_local_instrument_master(
        evidence_directory=evidence,
        instrument_master_path=master,
        provenance_path=provenance,
        repository_root=tmp_path,
    )
    loaded = load_instrument_master(master)
    payload = json.loads(provenance.read_text(encoding="utf-8"))

    assert result.record_conflicts == 1
    assert loaded["ABC"].company_name == ""
    assert loaded["ABC"].verification_status.value == "UNVERIFIED_INSTRUMENT"
    assert payload["records"]["ABC"]["resolution_status"] == "VERIFICATION_CONFLICT"
    assert payload["conflicts"]["records"][0]["status"] == "VERIFICATION_CONFLICT"


def test_verified_equity_gate_rejects_partial_company_listing_metadata(tmp_path: Path) -> None:
    master = tmp_path / "master.csv"
    master.write_text(
        "trading_code,company_name,sector,instrument_type,market_category,listing_status,"
        "observed_at,source_reference,verification_status\n"
        "ABC,ABC Limited,,,,,,saved-company-list.html,VERIFIED_EQUITY\n",
        encoding="utf-8",
    )

    with pytest.raises(WatchtowerError, match="lacks required official metadata"):
        load_instrument_master(master)


def test_unverified_raw_anomaly_requests_profile_but_cannot_enter_verified_ranking() -> None:
    first = date(2026, 1, 1)
    sessions = tuple(
        DayEndSession(
            market_date=first + timedelta(days=offset),
            source_path=Path(f"day-{offset}.csv"),
            source_sha256=f"hash-{offset}",
            observations=(_observation(first + timedelta(days=offset), anomalous=offset == 40),),
        )
        for offset in range(41)
    )
    provenance = {
        "schema": "dse_watchtower_instrument_master_provenance@0.2.0",
        "summary": {"record_conflicts": 0, "source_summary_conflicts": 0},
        "records": {
            "ABC": {
                "resolution_status": "PARTIAL_METADATA",
                "official_profile_reference": "https://www.dsebd.org/displayCompany.php?name=ABC",
                "missing_fields": [
                    "sector",
                    "instrument_type",
                    "market_category",
                    "listing_status",
                    "observed_at",
                ],
            }
        },
    }

    report = build_watchtower_report(sessions, {}, (), instrument_provenance=provenance)
    record = report["records"][0]
    request = report["profile_evidence_required"][0]

    assert record["report_label"] == "DATA_ISSUE"
    assert record["watchlist_candidate_eligible"] is False
    assert record["attention_score"]["total"] >= 8
    assert report["rankings"]["verified_watchlist"] == []
    assert report["rankings"]["unverified_raw_anomalies"][0]["trading_code"] == "ABC"
    assert request["status"] == "PROFILE_EVIDENCE_REQUIRED"
    assert request["official_profile_reference"].endswith("name=ABC")
    assert request["network_fetch_performed"] is False
    assert request["missing_fields"] == [
        "sector",
        "instrument_type",
        "market_category",
        "listing_status",
        "observed_at",
    ]
