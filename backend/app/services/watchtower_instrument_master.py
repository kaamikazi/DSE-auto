from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from dataclasses import dataclass
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

MASTER_COLUMNS = (
    "trading_code",
    "company_name",
    "sector",
    "instrument_type",
    "market_category",
    "listing_status",
    "observed_at",
    "source_reference",
    "verification_status",
)
PROVENANCE_SCHEMA = "dse_watchtower_instrument_master_provenance@0.2.0"
SYMBOL_PATTERN = re.compile(r"^[A-Z0-9().&_-]{1,32}$")
COUNT_PATTERN = re.compile(r"Total\s+Company\s+List:\s*(\d+)", re.IGNORECASE)


class InstrumentMasterError(RuntimeError):
    """A fail-closed error for local official instrument evidence."""


@dataclass(frozen=True)
class CompanyListingRecord:
    trading_code: str
    company_name: str
    profile_reference: str


@dataclass(frozen=True)
class IndustrySummaryRecord:
    industry_name: str
    quantity: int
    detail_reference: str


@dataclass(frozen=True)
class ParsedCompanyListing:
    title: str
    declared_count: int | None
    records: tuple[CompanyListingRecord, ...]


@dataclass(frozen=True)
class ParsedIndustryListing:
    title: str
    records: tuple[IndustrySummaryRecord, ...]


@dataclass(frozen=True)
class InstrumentMasterBuild:
    master_rows: int
    company_records_extracted: int
    industry_summary_rows_extracted: int
    exact_code_sector_joins: int
    record_conflicts: int
    source_summary_conflicts: int
    master_sha256: str
    provenance_sha256: str
    source_sha256: dict[str, str]

    def payload(self) -> dict[str, Any]:
        return {
            "master_rows": self.master_rows,
            "company_records_extracted": self.company_records_extracted,
            "industry_summary_rows_extracted": self.industry_summary_rows_extracted,
            "exact_code_sector_joins": self.exact_code_sector_joins,
            "record_conflicts": self.record_conflicts,
            "source_summary_conflicts": self.source_summary_conflicts,
            "master_sha256": self.master_sha256,
            "provenance_sha256": self.provenance_sha256,
            "source_sha256": self.source_sha256,
        }


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _clean_text(parts: list[str]) -> str:
    return " ".join("".join(parts).replace("\xa0", " ").split())


def _official_url(value: str, *, endpoint: str) -> bool:
    parsed = urlparse(value)
    return (
        parsed.scheme == "https"
        and parsed.hostname in {"dsebd.org", "www.dsebd.org"}
        and parsed.path.rsplit("/", 1)[-1] == endpoint
    )


class _CompanyListingParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.heading_parts: list[str] = []
        self.anchor_parts: list[str] = []
        self.company_parts: list[str] = []
        self.in_title = False
        self.in_heading = False
        self.in_anchor = False
        self.in_company_span = False
        self.anchor_href: str | None = None
        self.pending: tuple[str, str] | None = None
        self.records: list[CompanyListingRecord] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key: value or "" for key, value in attrs}
        if tag == "title":
            self.in_title = True
        elif tag == "h2":
            self.in_heading = True
            self.heading_parts = []
        elif tag == "a":
            href = attributes.get("href", "")
            classes = set(attributes.get("class", "").split())
            if "ab1" in classes and _official_url(href, endpoint="displayCompany.php"):
                self.in_anchor = True
                self.anchor_href = href
                self.anchor_parts = []
        elif tag == "span" and self.pending is not None:
            self.in_company_span = True
            self.company_parts = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self.in_title = False
        elif tag == "h2":
            self.in_heading = False
        elif tag == "a" and self.in_anchor:
            code = _clean_text(self.anchor_parts).upper()
            href = self.anchor_href or ""
            raw_query = urlparse(href).query
            query_code = (
                unquote(raw_query.removeprefix("name=")).strip().upper()
                if raw_query.startswith("name=")
                else ""
            )
            if SYMBOL_PATTERN.fullmatch(code) and query_code == code:
                self.pending = (code, href)
            self.in_anchor = False
            self.anchor_href = None
            self.anchor_parts = []
        elif tag == "span" and self.in_company_span:
            name = _clean_text(self.company_parts)
            if name.startswith("(") and name.endswith(")"):
                name = name[1:-1].strip()
            if self.pending is not None and name:
                code, href = self.pending
                self.records.append(CompanyListingRecord(code, name, href))
            self.pending = None
            self.in_company_span = False
            self.company_parts = []

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.title_parts.append(data)
        if self.in_heading:
            self.heading_parts.append(data)
        if self.in_anchor:
            self.anchor_parts.append(data)
        if self.in_company_span:
            self.company_parts.append(data)


class _IndustryListingParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.in_title = False
        self.in_row = False
        self.in_cell = False
        self.cell_parts: list[str] = []
        self.cells: list[str] = []
        self.detail_reference: str | None = None
        self.records: list[IndustrySummaryRecord] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key: value or "" for key, value in attrs}
        if tag == "title":
            self.in_title = True
        elif tag == "tr":
            self.in_row = True
            self.cells = []
            self.detail_reference = None
        elif tag in {"td", "th"} and self.in_row:
            self.in_cell = True
            self.cell_parts = []
        elif tag == "a" and self.in_row:
            href = attributes.get("href", "")
            if _official_url(href, endpoint="companylistbyindustry.php"):
                query = parse_qs(urlparse(href).query)
                if len(query.get("industryno", [])) == 1:
                    self.detail_reference = href

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self.in_title = False
        elif tag in {"td", "th"} and self.in_cell:
            self.cells.append(_clean_text(self.cell_parts))
            self.in_cell = False
            self.cell_parts = []
        elif tag == "tr" and self.in_row:
            if (
                len(self.cells) >= 3
                and self.cells[0].isdigit()
                and self.cells[2].isdigit()
                and self.detail_reference is not None
            ):
                self.records.append(
                    IndustrySummaryRecord(
                        industry_name=self.cells[1],
                        quantity=int(self.cells[2]),
                        detail_reference=self.detail_reference,
                    )
                )
            self.in_row = False

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.title_parts.append(data)
        if self.in_cell:
            self.cell_parts.append(data)


def parse_company_listing_html(text: str) -> ParsedCompanyListing:
    parser = _CompanyListingParser()
    parser.feed(text)
    title = _clean_text(parser.title_parts)
    count_match = COUNT_PATTERN.search(unescape(text))
    return ParsedCompanyListing(
        title=title,
        declared_count=int(count_match.group(1)) if count_match else None,
        records=tuple(parser.records),
    )


def parse_industry_listing_html(text: str) -> ParsedIndustryListing:
    parser = _IndustryListingParser()
    parser.feed(text)
    return ParsedIndustryListing(
        title=_clean_text(parser.title_parts),
        records=tuple(parser.records),
    )


def _portable_path(path: Path, repository_root: Path) -> str:
    try:
        return path.resolve().relative_to(repository_root.resolve()).as_posix()
    except ValueError:
        return path.name


def _discover_sources(
    evidence_directory: Path,
) -> tuple[Path, ParsedCompanyListing, Path, ParsedIndustryListing]:
    if not evidence_directory.is_dir():
        raise InstrumentMasterError(f"Instrument evidence directory is missing: {evidence_directory}")
    company: list[tuple[Path, ParsedCompanyListing]] = []
    industry: list[tuple[Path, ParsedIndustryListing]] = []
    for path in sorted(evidence_directory.iterdir(), key=lambda item: item.name.casefold()):
        if not path.is_file() or path.suffix.lower() not in {".html", ".htm"}:
            continue
        text = path.read_text(encoding="utf-8-sig", errors="strict")
        company_candidate = parse_company_listing_html(text)
        industry_candidate = parse_industry_listing_html(text)
        if "Company Listing" in company_candidate.title and company_candidate.records:
            company.append((path, company_candidate))
        if "Sector wise Company List" in industry_candidate.title and industry_candidate.records:
            industry.append((path, industry_candidate))
    if len(company) != 1 or len(industry) != 1:
        raise InstrumentMasterError(
            "Content discovery requires exactly one company listing and one industry listing "
            f"(found company={len(company)}, industry={len(industry)})"
        )
    return company[0][0], company[0][1], industry[0][0], industry[0][1]


def _canonical_json_bytes(payload: dict[str, Any]) -> bytes:
    return (
        json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def _master_bytes(rows: list[dict[str, str]]) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=MASTER_COLUMNS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue().encode("utf-8")


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def build_local_instrument_master(
    *,
    evidence_directory: Path,
    instrument_master_path: Path,
    provenance_path: Path,
    repository_root: Path,
) -> InstrumentMasterBuild:
    company_path, company_page, industry_path, industry_page = _discover_sources(
        evidence_directory
    )
    source_paths = (company_path, industry_path)
    source_hashes_before = {
        _portable_path(path, repository_root): sha256_file(path) for path in source_paths
    }

    grouped: dict[str, list[CompanyListingRecord]] = {}
    for record in company_page.records:
        grouped.setdefault(record.trading_code, []).append(record)

    conflicts: list[dict[str, Any]] = []
    rows: list[dict[str, str]] = []
    record_provenance: dict[str, dict[str, Any]] = {}
    company_source = _portable_path(company_path, repository_root)
    for code in sorted(grouped):
        variants = sorted(
            {(item.company_name, item.profile_reference) for item in grouped[code]}
        )
        is_conflict = len(variants) != 1
        if is_conflict:
            conflicts.append(
                {
                    "trading_code": code,
                    "status": "VERIFICATION_CONFLICT",
                    "variants": [
                        {"company_name": name, "profile_reference": profile}
                        for name, profile in variants
                    ],
                }
            )
            company_name = ""
            profile_reference = ""
            resolution_status = "VERIFICATION_CONFLICT"
        else:
            company_name, profile_reference = variants[0]
            resolution_status = "PARTIAL_METADATA"
        rows.append(
            {
                "trading_code": code,
                "company_name": company_name,
                "sector": "",
                "instrument_type": "",
                "market_category": "",
                "listing_status": "",
                "observed_at": "",
                "source_reference": company_source,
                "verification_status": "UNVERIFIED_INSTRUMENT",
            }
        )
        record_provenance[code] = {
            "resolution_status": resolution_status,
            "verification_status": "UNVERIFIED_INSTRUMENT",
            "ordinary_equity_proven": False,
            "observed_at": None,
            "company_name": company_name,
            "official_profile_reference": profile_reference,
            "evidence_source_ids": ["company_listing"],
            "conflicts": (
                [
                    {
                        "status": "VERIFICATION_CONFLICT",
                        "variants": [
                            {"company_name": name, "profile_reference": profile}
                            for name, profile in variants
                        ],
                    }
                ]
                if is_conflict
                else []
            ),
            "field_provenance": {
                "trading_code": "company_listing",
                "company_name": "company_listing" if company_name else None,
                "profile_reference": "company_listing" if profile_reference else None,
            },
            "missing_fields": [
                "sector",
                "instrument_type",
                "market_category",
                "listing_status",
                "observed_at",
            ],
        }

    industry_total = sum(record.quantity for record in industry_page.records)
    source_summary_conflicts: list[dict[str, Any]] = []
    if company_page.declared_count is None:
        source_summary_conflicts.append(
            {"status": "VERIFICATION_CONFLICT", "reason": "company_declared_count_missing"}
        )
    else:
        compared = {
            "company_declared_count": company_page.declared_count,
            "company_unique_codes": len(grouped),
            "industry_quantity_total": industry_total,
        }
        if len(set(compared.values())) != 1:
            source_summary_conflicts.append(
                {
                    "status": "VERIFICATION_CONFLICT",
                    "reason": "source_universe_counts_disagree",
                    **compared,
                }
            )

    master_payload = _master_bytes(rows)
    master_sha256 = _sha256_bytes(master_payload)
    source_objects = [
        {
            "source_id": "company_listing",
            "source_type": "official_company_listing",
            "path": company_source,
            "sha256": source_hashes_before[company_source],
            "page_title": company_page.title,
            "declared_count": company_page.declared_count,
            "records_extracted": len(company_page.records),
            "unique_codes": len(grouped),
            "operator_observed_at": None,
            "fields_supported": ["trading_code", "company_name", "profile_reference"],
            "fields_not_supported": [
                "sector",
                "instrument_type",
                "market_category",
                "listing_status",
            ],
        },
        {
            "source_id": "industry_summary",
            "source_type": "official_industry_summary",
            "path": _portable_path(industry_path, repository_root),
            "sha256": source_hashes_before[_portable_path(industry_path, repository_root)],
            "page_title": industry_page.title,
            "records_extracted": len(industry_page.records),
            "quantity_total": industry_total,
            "operator_observed_at": None,
            "fields_supported": ["industry_name", "quantity", "detail_reference"],
            "fields_not_supported": ["trading_code_to_sector"],
        },
    ]
    provenance: dict[str, Any] = {
        "schema": PROVENANCE_SCHEMA,
        "policy": {
            "resolution": "exact_trading_code_only",
            "fuzzy_matching_used": False,
            "guessed_metadata_used": False,
            "day_end_presence_proves_equity": False,
            "network_used": False,
        },
        "sources": source_objects,
        "industry_summary": [
            {
                "industry_name": record.industry_name,
                "quantity": record.quantity,
                "detail_reference": record.detail_reference,
            }
            for record in industry_page.records
        ],
        "summary": {
            "master_rows": len(rows),
            "company_records_extracted": len(company_page.records),
            "company_unique_codes": len(grouped),
            "industry_summary_rows_extracted": len(industry_page.records),
            "industry_quantity_total": industry_total,
            "exact_code_sector_joins": 0,
            "verified_equities": 0,
            "non_equities": 0,
            "unverified_instruments": len(rows),
            "record_conflicts": len(conflicts),
            "source_summary_conflicts": len(source_summary_conflicts),
        },
        "conflicts": {
            "records": conflicts,
            "source_summaries": source_summary_conflicts,
        },
        "records": record_provenance,
        "master": {
            "path": _portable_path(instrument_master_path, repository_root),
            "sha256": master_sha256,
        },
    }
    provenance_payload = _canonical_json_bytes(provenance)
    source_hashes_after = {
        _portable_path(path, repository_root): sha256_file(path) for path in source_paths
    }
    if source_hashes_after != source_hashes_before:
        raise InstrumentMasterError("Operator-owned instrument evidence changed during build")
    instrument_master_path.parent.mkdir(parents=True, exist_ok=True)
    provenance_path.parent.mkdir(parents=True, exist_ok=True)
    instrument_master_path.write_bytes(master_payload)
    provenance_path.write_bytes(provenance_payload)
    return InstrumentMasterBuild(
        master_rows=len(rows),
        company_records_extracted=len(company_page.records),
        industry_summary_rows_extracted=len(industry_page.records),
        exact_code_sector_joins=0,
        record_conflicts=len(conflicts),
        source_summary_conflicts=len(source_summary_conflicts),
        master_sha256=master_sha256,
        provenance_sha256=_sha256_bytes(provenance_payload),
        source_sha256=source_hashes_before,
    )


def load_instrument_provenance(path: Path | None) -> dict[str, Any]:
    if path is None or not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise InstrumentMasterError("Instrument provenance is not valid JSON") from exc
    if not isinstance(payload, dict) or payload.get("schema") != PROVENANCE_SCHEMA:
        raise InstrumentMasterError("Instrument provenance schema is not supported")
    records = payload.get("records")
    if not isinstance(records, dict):
        raise InstrumentMasterError("Instrument provenance records are invalid")
    return payload
