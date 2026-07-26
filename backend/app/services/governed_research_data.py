from __future__ import annotations

import csv
import hashlib
import io
import json
import mimetypes
import re
import zipfile
from collections import Counter, defaultdict
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from html import escape
from pathlib import Path, PurePosixPath
from typing import Any, cast

import pandas as pd  # type: ignore[import-untyped]
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.models import (
    CorporateActionRecord,
    CrossSourceValidationRun,
    DatasetImportRun,
    GovernedDataset,
    NormalizedDailyBar,
    Order,
    PortfolioStatementDraft,
    ResearchUniverseVersion,
    StrategyRegistration,
    Transaction,
    UniverseMembershipPeriod,
    ValidationCampaign,
)
from app.services.audit import append_audit, verify_audit_chain
from app.services.authoritative_evidence import canonical_hash

SOURCE_CATEGORIES = {
    "mendeley",
    "kaggle",
    "dse_stocks_archive",
    "amarstock",
    "manual_broker",
    "manual_exchange",
    "licensed_vendor",
    "dsex_index",
}
SOURCE_TRUST = {
    "exchange_verified",
    "licensed_vendor",
    "official_document",
    "third_party_research",
    "operator_attested",
    "unknown",
}
TIMESTAMP_TRUST = {
    "exchange_verified",
    "provider_asserted",
    "operator_attested",
    "receipt_only",
    "unknown",
}
ADJUSTMENT_STATUS = {"adjusted", "unadjusted", "mixed", "unknown"}
ALLOWED_SUFFIXES = {".csv", ".zip", ".xlsx", ".json", ".parquet"}
MAX_RAW_BYTES = 128 * 1024 * 1024
MAX_ARCHIVE_FILE_BYTES = 25 * 1024 * 1024
MAX_ARCHIVE_TOTAL_BYTES = 100 * 1024 * 1024
MAX_ARCHIVE_FILES = 50
MAX_COMPRESSION_RATIO = 200
SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")
REQUIRED_FIELDS = {"symbol", "trading_date", "open", "high", "low", "close", "volume"}
OPTIONAL_FIELDS = {
    "adjusted_close",
    "value",
    "number_of_trades",
    "previous_close",
    "exchange_event_timestamp",
    "source_publication_timestamp",
    "provider_update_timestamp",
    "file_generation_timestamp",
    "operator_download_timestamp",
    "system_receipt_timestamp",
}
COMPARISON_FIELDS = (
    "open",
    "high",
    "low",
    "close",
    "adjusted_close",
    "volume",
    "value",
    "number_of_trades",
    "previous_close",
)
CORPORATE_ACTION_TYPES = {
    "cash_dividend",
    "stock_dividend",
    "bonus_share",
    "rights_issue",
    "stock_split",
    "reverse_split",
    "symbol_change",
    "merger",
    "delisting",
    "suspension",
}
VENDOR_QUESTIONS = [
    "Are timestamps exchange events, provider updates, or receipt times?",
    "Is the feed real-time or delayed, and by how much?",
    "Is Level 1 or Level 2 depth supplied?",
    "What historical depth and revision policy apply?",
    "How are corporate actions and DSEX values sourced?",
    "Are market status and suspension events included?",
    "What latency, rate limits, authentication, SLA, sandbox, and support apply?",
    "What licensing and redistribution rights apply?",
]
BROKER_QUESTIONS = [
    "Which CSV/XLSX exports are available?",
    "Is there a documented read-only API?",
    "Is any trading or FIX API available to this client class?",
    "How are order states, fills, cancellations, and rejections timestamped?",
    "Are portfolio and settlement balances exposed separately?",
    "Is market data supplied and what is its timestamp provenance?",
    "Is a sandbox available and is automation contractually permitted?",
    "Which client approvals are required and what is the exact effective fee schedule?",
]


def _safe_filename(filename: str) -> str:
    name = SAFE_NAME.sub("_", Path(filename).name).strip("._")
    if not name or Path(name).suffix.lower() not in ALLOWED_SUFFIXES:
        raise ValueError("Unsupported or unsafe dataset filename")
    return name[:180]


def _reject_active_content(raw: bytes) -> None:
    if not raw or len(raw) > MAX_RAW_BYTES:
        raise ValueError("Dataset is empty or exceeds the raw-file limit")
    if raw.startswith((b"MZ", b"\x7fELF")) or b"<script" in raw[:4096].lower():
        raise ValueError("Executable or active content is prohibited")


def _archive_members(raw: bytes) -> list[tuple[str, bytes]]:
    members: list[tuple[str, bytes]] = []
    total = 0
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        infos = archive.infolist()
        if len(infos) > MAX_ARCHIVE_FILES:
            raise ValueError("Archive contains too many files")
        for info in infos:
            path = PurePosixPath(info.filename.replace("\\", "/"))
            if info.is_dir():
                continue
            if path.is_absolute() or ".." in path.parts or len(path.parts) != 1:
                raise ValueError("Archive path traversal or nested content is prohibited")
            if Path(path.name).suffix.lower() != ".csv":
                raise ValueError("ZIP archives may contain CSV files only")
            if (info.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError("Archive links are prohibited")
            if info.file_size > MAX_ARCHIVE_FILE_BYTES:
                raise ValueError("Archive member exceeds the size limit")
            ratio = info.file_size / max(info.compress_size, 1)
            if ratio > MAX_COMPRESSION_RATIO:
                raise ValueError("Archive compression ratio is unsafe")
            total += info.file_size
            if total > MAX_ARCHIVE_TOTAL_BYTES:
                raise ValueError("Archive decompression limit exceeded")
            content = archive.read(info)
            _reject_active_content(content)
            members.append((_safe_filename(path.name), content))
    if not members:
        raise ValueError("Archive contains no CSV files")
    return members


def _rows(filename: str, raw: bytes) -> list[dict[str, Any]]:
    suffix = Path(filename).suffix.lower()
    if suffix == ".zip":
        result: list[dict[str, Any]] = []
        for member_name, content in _archive_members(raw):
            for row in _rows(member_name, content):
                row["_archive_member"] = member_name
                result.append(row)
        return result
    if suffix == ".csv":
        return [dict(row) for row in csv.DictReader(io.StringIO(raw.decode("utf-8-sig")))]
    if suffix == ".json":
        payload = json.loads(raw.decode("utf-8-sig"))
        if not isinstance(payload, list) or not all(isinstance(row, dict) for row in payload):
            raise ValueError("JSON datasets must contain an array of objects")
        return [dict(row) for row in payload]
    try:
        frame = (
            pd.read_excel(io.BytesIO(raw), dtype=str)
            if suffix == ".xlsx"
            else pd.read_parquet(io.BytesIO(raw))
        )
    except ImportError as exc:
        raise ValueError(f"Controlled dependency for {suffix} is unavailable") from exc
    records: object = json.loads(frame.fillna("").to_json(orient="records", date_format="iso"))
    if not isinstance(records, list) or not all(isinstance(row, dict) for row in records):
        raise ValueError("Tabular dataset conversion did not produce object rows")
    return [dict(row) for row in records]


def register_dataset(
    db: Session,
    *,
    filename: str,
    raw: bytes,
    raw_dir: Path,
    source_category: str,
    source_name: str,
    source_reference: str,
    publisher: str,
    license_note: str,
    operator: str,
    timestamp_trust: str,
    source_trust: str,
    publication_date: date | None = None,
    stated_date_coverage: str = "",
    stated_symbol_coverage: list[str] | None = None,
    adjustment_status: str = "unknown",
    notes: str = "",
) -> GovernedDataset:
    if source_category not in SOURCE_CATEGORIES:
        raise ValueError("Unknown dataset source category")
    if source_trust not in SOURCE_TRUST or timestamp_trust not in TIMESTAMP_TRUST:
        raise ValueError("Unknown trust classification")
    if adjustment_status not in ADJUSTMENT_STATUS:
        raise ValueError("Unknown adjustment status")
    if source_trust == "exchange_verified" and source_category not in {
        "manual_exchange",
        "dsex_index",
    }:
        raise ValueError("Domain/category alone cannot confer exchange verification")
    if len(license_note.strip()) < 5 or len(operator.strip()) < 2:
        raise ValueError("License note and operator are required")
    safe_name = _safe_filename(filename)
    _reject_active_content(raw)
    if Path(safe_name).suffix.lower() == ".zip":
        from app.services.public_source_collection import validate_archive_bytes

        validate_archive_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    duplicate = db.scalar(select(GovernedDataset).where(GovernedDataset.raw_sha256 == digest))
    if duplicate:
        raise ValueError(f"Duplicate dataset file: {duplicate.id}")
    retained = raw_dir / digest[:2] / digest / safe_name
    retained.parent.mkdir(parents=True, exist_ok=True)
    if retained.exists() and retained.read_bytes() != raw:
        raise ValueError("Immutable raw-retention collision")
    if not retained.exists():
        retained.write_bytes(raw)
    item = GovernedDataset(
        source_category=source_category,
        source_name=source_name,
        source_reference=source_reference,
        publisher=publisher,
        publication_date=publication_date,
        stated_date_coverage=stated_date_coverage,
        stated_symbol_coverage=sorted({s.upper() for s in stated_symbol_coverage or []}),
        license_note=license_note,
        adjustment_status=adjustment_status,
        raw_sha256=digest,
        file_size=len(raw),
        mime_type=mimetypes.guess_type(safe_name)[0] or "application/octet-stream",
        raw_file_path=str(retained),
        operator=operator,
        timestamp_trust=timestamp_trust,
        source_trust=source_trust,
        review_status="registered",
        notes=notes,
    )
    db.add(item)
    db.flush()
    event = append_audit(
        db,
        actor=operator,
        event_type="research_dataset.registered",
        entity_type="governed_dataset",
        entity_id=item.id,
        new_state={"sha256": digest, "source_trust": source_trust, "activated": False},
    )
    item.audit_event_ids = [event.id]
    db.commit()
    return item


def preview_import(
    db: Session,
    dataset: GovernedDataset,
    *,
    column_mapping: dict[str, str],
) -> DatasetImportRun:
    if set(column_mapping) - (REQUIRED_FIELDS | OPTIONAL_FIELDS):
        raise ValueError("Column mapping contains unknown normalized fields")
    missing_mapping = REQUIRED_FIELDS - set(column_mapping)
    if missing_mapping:
        raise ValueError(f"Required mappings missing: {', '.join(sorted(missing_mapping))}")
    raw = Path(dataset.raw_file_path).read_bytes()
    source_rows = _rows(Path(dataset.raw_file_path).name, raw)
    if not source_rows:
        raise ValueError("Dataset contains no rows")
    headers = sorted(
        {str(key) for row in source_rows for key in row if not str(key).startswith("_")}
    )
    missing_columns = sorted({value for value in column_mapping.values() if value not in headers})
    if missing_columns:
        raise ValueError(f"Mapped columns not present: {', '.join(missing_columns)}")
    errors: list[dict[str, Any]] = []
    normalized: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for index, row in enumerate(source_rows, 1):
        try:
            item = _normalize_row(dataset, row, column_mapping, index)
            key = (item["symbol"], item["trading_date"])
            if key in seen:
                errors.append({"row": index, "classification": "duplicate"})
            seen.add(key)
            if not _valid_ohlc(item):
                errors.append({"row": index, "classification": "invalid_ohlc"})
            normalized.append(item)
        except (KeyError, ValueError, InvalidOperation) as exc:
            errors.append({"row": index, "classification": "invalid", "detail": str(exc)})
    batch_hash = canonical_hash(
        {"raw_sha256": dataset.raw_sha256, "mapping": column_mapping, "normalized": normalized}
    )
    duplicate = db.scalar(select(DatasetImportRun).where(DatasetImportRun.batch_hash == batch_hash))
    if duplicate:
        raise ValueError(f"Duplicate import batch: {duplicate.id}")
    preview = {
        "headers": headers,
        "sample": normalized[:10],
        "symbols": sorted({row["symbol"] for row in normalized}),
        "date_start": min((row["trading_date"] for row in normalized), default=None),
        "date_end": max((row["trading_date"] for row in normalized), default=None),
        "timestamp_types_kept_separate": True,
        "research_only": True,
    }
    run = DatasetImportRun(
        dataset_id=dataset.id,
        batch_hash=batch_hash,
        column_mapping=column_mapping,
        inferred_schema={header: type(source_rows[0].get(header)).__name__ for header in headers},
        preview=preview,
        state="review_required" if errors else "previewed",
        row_count=len(normalized),
        errors=errors,
    )
    db.add(run)
    db.flush()
    append_audit(
        db,
        actor=dataset.operator,
        event_type="research_dataset.import_previewed",
        entity_type="dataset_import_run",
        entity_id=run.id,
        new_state={"batch_hash": batch_hash, "rows": len(normalized), "errors": len(errors)},
    )
    db.commit()
    return run


def _normalize_row(
    dataset: GovernedDataset, row: dict[str, Any], mapping: dict[str, str], index: int
) -> dict[str, Any]:
    def get(field: str) -> Any:
        return row.get(mapping[field], "") if field in mapping else ""

    result: dict[str, Any] = {
        "symbol": str(get("symbol")).strip().upper(),
        "trading_date": date.fromisoformat(str(get("trading_date"))[:10]).isoformat(),
        "open": str(Decimal(str(get("open")).replace(",", ""))),
        "high": str(Decimal(str(get("high")).replace(",", ""))),
        "low": str(Decimal(str(get("low")).replace(",", ""))),
        "close": str(Decimal(str(get("close")).replace(",", ""))),
        "volume": str(Decimal(str(get("volume") or "0").replace(",", ""))),
        "source": dataset.source_name,
        "source_row_id": f"{row.get('_archive_member', 'root')}:{index}",
        "adjusted": dataset.adjustment_status == "adjusted",
        "timestamp_trust": dataset.timestamp_trust,
        "dataset_id": dataset.id,
    }
    if not result["symbol"]:
        raise ValueError("Symbol is empty")
    for field in ("adjusted_close", "value", "number_of_trades", "previous_close"):
        value = get(field)
        result[field] = (
            str(Decimal(str(value).replace(",", ""))) if value not in ("", None) else None
        )
    result["timestamp_provenance"] = {
        field: get(field) or None for field in OPTIONAL_FIELDS if field.endswith("timestamp")
    }
    return result


def _valid_ohlc(row: dict[str, Any]) -> bool:
    o, h, low, close = (Decimal(row[key]) for key in ("open", "high", "low", "close"))
    return min(o, close) >= low >= 0 and h >= max(o, close) and Decimal(row["volume"]) >= 0


def activate_for_research(
    db: Session, run: DatasetImportRun, *, operator: str, normalized_dir: Path
) -> DatasetImportRun:
    if run.state != "previewed" or run.errors:
        raise ValueError("Only a clean preview may be activated for research")
    dataset = db.get(GovernedDataset, run.dataset_id)
    if dataset is None or dataset.review_status not in {"registered", "approved_for_research"}:
        raise ValueError("Dataset registration is not eligible for research activation")
    raw = Path(dataset.raw_file_path).read_bytes()
    normalized = [
        _normalize_row(dataset, row, run.column_mapping, index)
        for index, row in enumerate(_rows(Path(dataset.raw_file_path).name, raw), 1)
    ]
    if any(not _valid_ohlc(row) for row in normalized):
        raise ValueError("Activation blocked by invalid OHLC")
    output = normalized_dir / dataset.id / f"{run.batch_hash}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(normalized, sort_keys=True, indent=2).encode()
    if output.exists() and output.read_bytes() != payload:
        raise ValueError("Immutable normalized-retention collision")
    output.write_bytes(payload)
    for item in normalized:
        db.add(
            NormalizedDailyBar(
                dataset_id=dataset.id,
                import_run_id=run.id,
                batch_hash=run.batch_hash,
                symbol=item["symbol"],
                trading_date=date.fromisoformat(item["trading_date"]),
                open=Decimal(item["open"]),
                high=Decimal(item["high"]),
                low=Decimal(item["low"]),
                close=Decimal(item["close"]),
                adjusted_close=Decimal(item["adjusted_close"]) if item["adjusted_close"] else None,
                volume=Decimal(item["volume"]),
                value=Decimal(item["value"]) if item["value"] else None,
                number_of_trades=int(Decimal(item["number_of_trades"]))
                if item["number_of_trades"]
                else None,
                previous_close=Decimal(item["previous_close"]) if item["previous_close"] else None,
                source=item["source"],
                source_row_id=item["source_row_id"],
                adjusted=item["adjusted"],
                timestamp_trust=item["timestamp_trust"],
                timestamp_provenance=item["timestamp_provenance"],
                active_for_research=True,
            )
        )
    run.normalized_file_path = str(output)
    run.state = "active_for_research"
    run.activated_at = datetime.now(UTC)
    dataset.review_status = "approved_for_research"
    append_audit(
        db,
        actor=operator,
        event_type="research_dataset.activated_for_research",
        entity_type="dataset_import_run",
        entity_id=run.id,
        new_state={"rows": len(normalized), "campaign_eligible": False, "order_eligible": False},
    )
    db.commit()
    return run


def rollback_import(db: Session, run: DatasetImportRun, *, operator: str) -> DatasetImportRun:
    if run.state == "rolled_back":
        raise ValueError("Import already rolled back")
    db.execute(delete(NormalizedDailyBar).where(NormalizedDailyBar.import_run_id == run.id))
    run.state = "rolled_back"
    run.rolled_back_at = datetime.now(UTC)
    append_audit(
        db,
        actor=operator,
        event_type="research_dataset.import_rolled_back",
        entity_type="dataset_import_run",
        entity_id=run.id,
        new_state={"bars_active": 0, "orders_affected": 0},
    )
    db.commit()
    return run


def compare_sources(
    db: Session,
    primary_dataset_id: str,
    secondary_dataset_id: str,
    *,
    output_dir: Path,
    tolerance: Decimal = Decimal("0.001"),
) -> CrossSourceValidationRun:
    def keyed(dataset_id: str) -> dict[tuple[str, date], list[NormalizedDailyBar]]:
        result: dict[tuple[str, date], list[NormalizedDailyBar]] = defaultdict(list)
        for bar in db.scalars(
            select(NormalizedDailyBar).where(
                NormalizedDailyBar.dataset_id == dataset_id,
                NormalizedDailyBar.active_for_research.is_(True),
            )
        ):
            result[(bar.symbol, bar.trading_date)].append(bar)
        return result

    primary, secondary = keyed(primary_dataset_id), keyed(secondary_dataset_id)
    ledger: list[dict[str, Any]] = []
    for key in sorted(set(primary) | set(secondary)):
        p, s = primary.get(key, []), secondary.get(key, [])
        if not p:
            classification = "missing_primary"
        elif not s:
            classification = "missing_secondary"
        elif len(p) > 1 or len(s) > 1:
            classification = "duplicate"
        elif not _bar_valid(p[0]) or not _bar_valid(s[0]):
            classification = "invalid_ohlc"
        else:
            differences = [
                _relative_difference(getattr(p[0], f), getattr(s[0], f)) for f in COMPARISON_FIELDS
            ]
            if all(diff == 0 for diff in differences if diff is not None):
                classification = "exact_match"
            elif all(diff is None or diff <= tolerance for diff in differences):
                classification = "within_tolerance"
            elif _price_jump_suggests_action(p[0], s[0]):
                classification = "corporate_action_suspected"
            else:
                classification = "material_conflict"
        ledger.append(
            {"symbol": key[0], "trading_date": key[1].isoformat(), "classification": classification}
        )
    counts = Counter(row["classification"] for row in ledger)
    scores = _quality_scores(ledger)
    report = {
        "primary_dataset_id": primary_dataset_id,
        "secondary_dataset_id": secondary_dataset_id,
        "counts": dict(counts),
        "ledger": ledger,
        **scores,
        "prices_averaged": False,
        "human_review_required": counts["material_conflict"] > 0,
    }
    digest = canonical_hash(report)
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / f"{digest}.json"
    csv_path = output_dir / f"{digest}.csv"
    html_path = output_dir / f"{digest}.html"
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["symbol", "trading_date", "classification"])
        writer.writeheader()
        writer.writerows(ledger)
    html_path.write_text(_validation_html(report), encoding="utf-8")
    run = CrossSourceValidationRun(
        primary_dataset_id=primary_dataset_id,
        secondary_dataset_id=secondary_dataset_id,
        report=report,
        report_hash=digest,
        output_paths={"json": str(json_path), "csv": str(csv_path), "html": str(html_path)},
        review_status="review_required" if report["human_review_required"] else "generated",
    )
    db.add(run)
    db.commit()
    return run


def _bar_valid(bar: NormalizedDailyBar) -> bool:
    return cast(bool, bar.low <= min(bar.open, bar.close) and bar.high >= max(bar.open, bar.close))


def _relative_difference(left: Any, right: Any) -> Decimal | None:
    if left is None or right is None:
        return None if left is right else Decimal("1")
    a, b = Decimal(str(left)), Decimal(str(right))
    return abs(a - b) / max(abs(a), abs(b), Decimal("0.0001"))


def _price_jump_suggests_action(left: NormalizedDailyBar, right: NormalizedDailyBar) -> bool:
    difference = _relative_difference(left.close, right.close)
    return difference is not None and difference >= Decimal("0.20")


def _quality_scores(ledger: list[dict[str, Any]]) -> dict[str, Any]:
    penalty = {
        "material_conflict": 30,
        "missing_primary": 20,
        "missing_secondary": 20,
        "invalid_ohlc": 30,
        "duplicate": 15,
        "corporate_action_suspected": 10,
    }

    def score(rows: list[dict[str, Any]]) -> int:
        return max(
            0, 100 - sum(penalty.get(row["classification"], 0) for row in rows) // max(len(rows), 1)
        )

    by_symbol: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_date: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in ledger:
        by_symbol[row["symbol"]].append(row)
        by_date[row["trading_date"]].append(row)
    return {
        "symbol_quality": {k: score(v) for k, v in by_symbol.items()},
        "date_quality": {k: score(v) for k, v in by_date.items()},
        "dataset_quality": score(ledger),
    }


def _validation_html(report: dict[str, Any]) -> str:
    rows = "".join(
        f"<tr><td>{escape(row['symbol'])}</td><td>{row['trading_date']}</td><td>{row['classification']}</td></tr>"
        for row in report["ledger"]
    )
    return f"<!doctype html><html><body><h1>Cross-source validation</h1><p>Prices averaged: no</p><p>Quality: {report['dataset_quality']}</p><table><tr><th>Symbol</th><th>Date</th><th>Classification</th></tr>{rows}</table></body></html>"


def register_corporate_action(
    db: Session, *, symbol: str, event_type: str, inferred: bool = False, **values: Any
) -> CorporateActionRecord:
    if event_type not in CORPORATE_ACTION_TYPES:
        raise ValueError("Unknown corporate-action type")
    if inferred and values.get("verification_status") == "verified":
        raise ValueError("Inferred corporate actions cannot be automatically verified")
    item = CorporateActionRecord(
        symbol=symbol.upper(),
        event_type=event_type,
        inferred=inferred,
        verification_status=values.get("verification_status", "pending"),
        review_decision="review_required",
        announcement_date=values.get("announcement_date"),
        ex_date=values.get("ex_date"),
        record_date=values.get("record_date"),
        effective_date=values.get("effective_date"),
        ratio_or_amount=values.get("ratio_or_amount"),
        source_evidence_ids=values.get("source_evidence_ids", []),
        adjustment_factor=values.get("adjustment_factor"),
        affected_dataset_ids=values.get("affected_dataset_ids", []),
    )
    db.add(item)
    db.commit()
    return item


def suspicious_discontinuities(
    bars: list[NormalizedDailyBar], threshold: Decimal = Decimal("0.20")
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for previous, current in zip(
        sorted(bars, key=lambda b: b.trading_date),
        sorted(bars, key=lambda b: b.trading_date)[1:],
        strict=False,
    ):
        change = abs(current.close / previous.close - 1) if previous.close else Decimal("0")
        if change >= threshold:
            result.append(
                {
                    "symbol": current.symbol,
                    "trading_date": current.trading_date.isoformat(),
                    "change": str(change),
                    "classification": "corporate_action_suspected",
                    "automatically_approved": False,
                }
            )
    return result


def create_universe(
    db: Session, *, name: str, memberships: list[dict[str, Any]]
) -> ResearchUniverseVersion:
    payload = {"name": name, "memberships": memberships, "survivorship_controlled": True}
    universe = ResearchUniverseVersion(
        name=name,
        status="draft",
        methodology={"eligibility_by_date": True, "automatic_activation": False},
        version_hash=canonical_hash(payload),
    )
    db.add(universe)
    db.flush()
    for item in memberships:
        db.add(
            UniverseMembershipPeriod(
                universe_id=universe.id,
                symbol=str(item["symbol"]).upper(),
                eligible_from=item["eligible_from"],
                eligible_to=item.get("eligible_to"),
                listing_date=item.get("listing_date"),
                delisting_date=item.get("delisting_date"),
                suspension_periods=item.get("suspension_periods", []),
                category_history=item.get("category_history", []),
                symbol_changes=item.get("symbol_changes", []),
                missing_data_periods=item.get("missing_data_periods", []),
                liquidity_history=item.get("liquidity_history", {}),
                sector=item.get("sector"),
                market_cap_metadata=item.get("market_cap_metadata", {}),
            )
        )
    db.commit()
    return universe


def eligible_on(membership: UniverseMembershipPeriod, when: date) -> bool:
    if when < membership.eligible_from or (
        membership.eligible_to and when > membership.eligible_to
    ):
        return False
    return not any(
        date.fromisoformat(period["from"]) <= when <= date.fromisoformat(period["to"])
        for period in membership.suspension_periods
    )


def portfolio_decision_support(
    draft: PortfolioStatementDraft,
    prices: dict[str, Decimal],
    sectors: dict[str, str] | None = None,
) -> dict[str, Any]:
    sectors = sectors or {}
    holdings = draft.parsed_data.get("holdings", [])
    rows: list[dict[str, Any]] = []
    total_value = Decimal(str(draft.parsed_data.get("cash_balance", "0")))
    warnings: list[dict[str, str]] = []
    for item in holdings:
        symbol = str(item["symbol"])
        quantity = Decimal(str(item["quantity"]))
        cost = Decimal(str(item["average_acquisition_cost"])) * quantity
        if symbol not in prices:
            warnings.append(
                {"type": "missing_data", "symbol": symbol, "classification": "review_required"}
            )
            value = cost
        else:
            value = prices[symbol] * quantity
        total_value += value
        rows.append(
            {
                "symbol": symbol,
                "sector": sectors.get(symbol, "unknown"),
                "quantity": str(quantity),
                "cost_basis": str(cost),
                "market_value": str(value),
                "unrealized_return": str((value / cost - 1) if cost else Decimal("0")),
            }
        )
    for row in rows:
        row["allocation"] = str(
            Decimal(row["market_value"]) / total_value if total_value else Decimal("0")
        )
        if Decimal(row["allocation"]) > Decimal("0.25"):
            warnings.append(
                {"type": "concentration", "symbol": row["symbol"], "classification": "risk_warning"}
            )
    return {
        "banner": "REAL PORTFOLIO — READ ONLY",
        "holdings": rows,
        "cash_allocation": str(
            Decimal(str(draft.parsed_data.get("cash_balance", "0"))) / total_value
            if total_value
            else 0
        ),
        "warnings": warnings,
        "benchmark_comparisons": {
            "DSEX": "requires governed benchmark data",
            "buy_and_hold": "research observation",
            "paper_strategy": "paper scenario",
        },
        "orders_created": 0,
        "instructions": False,
    }


def research_manifest(
    db: Session,
    *,
    dataset: GovernedDataset,
    universe: ResearchUniverseVersion,
    parameters: dict[str, Any],
    code_hash: str,
    rule_version: str,
    fee_version: str,
    corporate_action_version: str,
) -> dict[str, Any]:
    strategy = db.scalar(
        select(StrategyRegistration).where(
            StrategyRegistration.strategy_id == "ma_crossover",
            StrategyRegistration.version == "1.0.0",
        )
    )
    if strategy is None or strategy.lifecycle_state != "research":
        raise ValueError("ma_crossover@1.0.0 must remain registered as research")
    manifest = {
        "strategy": "ma_crossover@1.0.0",
        "status": "research",
        "code_hash": code_hash,
        "parameter_hash": canonical_hash(parameters),
        "dataset_hash": dataset.raw_sha256,
        "universe_version": universe.version_hash,
        "rule_assumption_version": rule_version,
        "fee_assumption_version": fee_version,
        "corporate_action_adjustment_version": corporate_action_version,
        "analyses": [
            "deterministic_backtest",
            "walk_forward",
            "rolling_out_of_sample",
            "parameter_sensitivity",
            "fee_sensitivity",
            "slippage_sensitivity",
            "turnover",
            "liquidity",
            "drawdown",
            "regime",
            "symbol",
            "sector",
            "DSEX",
            "buy_and_hold",
        ],
        "survivorship_bias_disclosed": True,
        "look_ahead_bias_check_required": True,
        "profit_guarantee": False,
        "promotion_allowed": False,
    }
    manifest["manifest_hash"] = canonical_hash(manifest)
    return manifest


def eod_research_workflow() -> dict[str, Any]:
    return {
        "steps": [
            "market_closed",
            "operator_download",
            "hash_and_retain",
            "import_preview",
            "cross_source_validation",
            "corporate_action_check",
            "operator_attestation",
            "research_only_activation",
            "research_signal",
            "portfolio_simulation",
            "risk_warnings",
            "human_review",
        ],
        "order_submission": False,
        "actual_trade": "outside_system_manual_owner_decision",
    }


def workspace_summary(db: Session) -> dict[str, Any]:
    def count(model: type[Any]) -> int:
        return int(db.scalar(select(func.count()).select_from(model)) or 0)

    strategies_promoted = int(
        db.scalar(
            select(func.count())
            .select_from(StrategyRegistration)
            .where(StrategyRegistration.lifecycle_state.in_({"paper_candidate", "paper_active"}))
        )
        or 0
    )
    return {
        "banners": ["PAPER TRADING", "LIVE TRADING DISABLED", "REAL PORTFOLIO READ ONLY"],
        "datasets": count(GovernedDataset),
        "imports": count(DatasetImportRun),
        "active_research_bars": int(
            db.scalar(
                select(func.count())
                .select_from(NormalizedDailyBar)
                .where(NormalizedDailyBar.active_for_research.is_(True))
            )
            or 0
        ),
        "cross_source_runs": count(CrossSourceValidationRun),
        "corporate_actions": count(CorporateActionRecord),
        "universes": count(ResearchUniverseVersion),
        "portfolio_drafts": count(PortfolioStatementDraft),
        "vendor_questionnaire": VENDOR_QUESTIONS,
        "broker_questionnaire": BROKER_QUESTIONS,
        "proof_no_activation": {
            "strategy_promotions": strategies_promoted,
            "campaigns": count(ValidationCampaign),
            "orders": count(Order),
            "fills_or_transactions": count(Transaction),
        },
        "qualification": "0/60",
        "audit_valid": verify_audit_chain(db),
    }
