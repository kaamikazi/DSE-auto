from __future__ import annotations

import csv
import hashlib
import io
import json
import mimetypes
import re
import ssl
import urllib.error
import urllib.request
import zipfile
from collections import Counter
from collections.abc import Iterable
from contextlib import suppress
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlparse

from sqlalchemy.orm import Session

from app.models import AuthoritativeEvidence, DatasetImportRun, ExtractedClaim, GovernedDataset
from app.services.audit import append_audit
from app.services.authoritative_evidence import canonical_hash

ATTEMPT_RESULTS = {
    "downloaded",
    "blocked_by_authentication",
    "blocked_by_license",
    "unavailable",
    "malformed",
    "duplicate",
    "manually_required",
}
APPROVED_HOSTS = {
    "data.mendeley.com",
    "static.data.mendeley.com",
    "prod-dcd-datasets-cache-zipfiles.s3.eu-west-1.amazonaws.com",
    "dsestocks.com",
    "www.dsestocks.com",
    "amarstock.com",
    "www.amarstock.com",
    "sec.gov.bd",
    "www.sec.gov.bd",
    "dsebd.org",
    "www.dsebd.org",
    "cdbl.com.bd",
    "www.cdbl.com.bd",
}
SAFE_MIME_TYPES = {
    "application/pdf",
    "application/zip",
    "text/csv",
    "text/plain",
}
MAX_DOWNLOAD_BYTES = 128 * 1024 * 1024
MAX_ARCHIVE_FILES = 2_000
MAX_ARCHIVE_TOTAL_BYTES = 1024 * 1024 * 1024
MAX_ARCHIVE_MEMBER_BYTES = 256 * 1024 * 1024
MAX_COMPRESSION_RATIO = 250
CSV_ALIASES = {
    "symbol": ("symbol", "trading_code", "scrip", "code"),
    "trading_date": ("trading_date", "date"),
    "open": ("open",),
    "high": ("high",),
    "low": ("low",),
    "close": ("close",),
    "volume": ("volume",),
}
RULE_TOPICS = {
    "trading_days": ("trading day", "business day"),
    "trading_hours": ("trading hour", "trading session"),
    "market_phases": ("market phase", "pre-opening", "closing price"),
    "holidays": ("holiday",),
    "tick_sizes": ("tick size", "minimum price movement"),
    "price_bands": ("price limit", "circuit breaker"),
    "settlement": ("settlement", "t+"),
    "suspensions": ("suspension", "suspend trading"),
    "corporate_actions": ("corporate action", "record date", "book closure"),
    "short_selling": ("short sale", "short selling"),
    "leverage": ("margin loan", "leverage", "margin limit"),
    "order_expiry": ("good till", "order expiry", "validity of order"),
    "fees_and_charges": ("fee", "charge", "commission"),
}


@dataclass(frozen=True)
class SourceAttempt:
    source_url: str
    publisher: str
    title: str
    result: str
    accessed_at: str
    license_note: str
    source_trust: str
    timestamp_trust: str
    publication_date: str | None = None
    local_path: str | None = None
    file_size: int | None = None
    mime_type: str | None = None
    sha256: str | None = None
    stated_date_coverage: str = ""
    stated_symbol_coverage: str = ""
    adjustment_status: str = "unknown"
    audit_linkage: list[str] | None = None
    detail: str = ""

    def __post_init__(self) -> None:
        if self.result not in ATTEMPT_RESULTS:
            raise ValueError("Each source attempt requires exactly one allowed result")
        if self.result == "downloaded" and not all(
            (self.local_path, self.file_size is not None, self.mime_type, self.sha256)
        ):
            raise ValueError("Downloaded attempts require complete file metadata")
        if self.source_trust == "exchange_verified":
            raise ValueError("Public research collection cannot infer exchange verification")


def validate_public_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Only credential-free HTTPS URLs are permitted")
    hostname = parsed.hostname.lower()
    if hostname not in APPROVED_HOSTS:
        raise ValueError(f"Source host is not approved: {hostname}")
    return hostname


def _payload_mime(filename: str, declared_mime: str, raw_prefix: bytes) -> str:
    mime = declared_mime.split(";", 1)[0].strip().lower()
    suffix = Path(filename).suffix.lower()
    if mime not in SAFE_MIME_TYPES:
        raise ValueError(f"Unsupported response MIME type: {mime}")
    if raw_prefix.startswith((b"MZ", b"\x7fELF")):
        raise ValueError("Executable content is prohibited")
    if suffix == ".pdf" and not raw_prefix.startswith(b"%PDF-"):
        raise ValueError("PDF signature mismatch")
    if suffix == ".zip" and not raw_prefix.startswith(b"PK\x03\x04"):
        raise ValueError("ZIP signature mismatch")
    if suffix in {".csv", ".txt"} and (
        b"<html" in raw_prefix.lower() or b"<script" in raw_prefix.lower()
    ):
        raise ValueError("HTML or active content cannot be retained as tabular evidence")
    guessed = mimetypes.guess_type(filename)[0] or mime
    if suffix == ".csv" and mime not in {"text/csv", "text/plain"}:
        raise ValueError("CSV MIME mismatch")
    return guessed


class _ApprovedRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        validate_public_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def safe_download(
    url: str,
    destination: Path,
    *,
    timeout_seconds: int = 30,
    max_bytes: int = MAX_DOWNLOAD_BYTES,
    request_data: bytes | None = None,
) -> dict[str, Any]:
    validate_public_url(url)
    context = ssl.create_default_context()
    opener = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=context), _ApprovedRedirectHandler()
    )
    request = urllib.request.Request(
        url, data=request_data, headers={"User-Agent": "DSE-AutoTrader-Evidence/1.0"}
    )
    try:
        response = opener.open(request, timeout=timeout_seconds)
    except urllib.error.HTTPError as exc:
        if exc.code in {401, 403}:
            raise PermissionError("Source requires authorization or denied public access") from exc
        raise
    final_url = response.geturl()
    validate_public_url(final_url)
    announced = response.headers.get("Content-Length")
    if announced and int(announced) > max_bytes:
        raise ValueError("Download exceeds the configured size limit")
    destination.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    size = 0
    prefix = b""
    temporary = destination.with_suffix(destination.suffix + ".partial")
    try:
        with temporary.open("wb") as handle:
            while chunk := response.read(1024 * 1024):
                size += len(chunk)
                if size > max_bytes:
                    raise ValueError("Download exceeded the configured size limit")
                if len(prefix) < 4096:
                    prefix += chunk[: 4096 - len(prefix)]
                digest.update(chunk)
                handle.write(chunk)
        mime = _payload_mime(destination.name, response.headers.get_content_type(), prefix)
        if destination.exists():
            existing_hash = hashlib.sha256(destination.read_bytes()).hexdigest()
            if existing_hash != digest.hexdigest():
                raise ValueError("Immutable download collision")
            temporary.unlink()
        else:
            temporary.replace(destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return {
        "source_url": url,
        "final_url": final_url,
        "file_size": size,
        "mime_type": mime,
        "sha256": digest.hexdigest(),
    }


def _validate_archive(archive: zipfile.ZipFile) -> dict[str, Any]:
    total = 0
    suffixes: Counter[str] = Counter()
    names: list[str] = []
    infos = [item for item in archive.infolist() if not item.is_dir()]
    if not infos or len(infos) > MAX_ARCHIVE_FILES:
        raise ValueError("Archive file count is empty or unsafe")
    for info in infos:
        member = PurePosixPath(info.filename.replace("\\", "/"))
        if member.is_absolute() or ".." in member.parts:
            raise ValueError("Archive path traversal is prohibited")
        if (info.external_attr >> 16) & 0o170000 == 0o120000:
            raise ValueError("Archive links are prohibited")
        if info.file_size > MAX_ARCHIVE_MEMBER_BYTES:
            raise ValueError("Archive member exceeds the safe size limit")
        if info.file_size / max(info.compress_size, 1) > MAX_COMPRESSION_RATIO:
            raise ValueError("Archive compression ratio is unsafe")
        suffix = member.suffix.lower()
        if suffix not in {".csv", ".txt"}:
            raise ValueError(f"Archive member type is not permitted: {suffix}")
        total += info.file_size
        if total > MAX_ARCHIVE_TOTAL_BYTES:
            raise ValueError("Archive expanded size exceeds the safe limit")
        suffixes[suffix] += 1
        names.append(info.filename)
    return {
        "member_count": len(names),
        "expanded_bytes": total,
        "member_types": dict(suffixes),
        "first_members": names[:20],
    }


def validate_archive_manifest(path: Path) -> dict[str, Any]:
    with zipfile.ZipFile(path) as archive:
        return _validate_archive(archive)


def validate_archive_bytes(raw: bytes) -> dict[str, Any]:
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        return _validate_archive(archive)


def _normalized_headers(fieldnames: Iterable[str]) -> dict[str, str]:
    available = {re.sub(r"[^a-z0-9]+", "_", item.lower()).strip("_"): item for item in fieldnames}
    mapping: dict[str, str] = {}
    for normalized, aliases in CSV_ALIASES.items():
        for alias in aliases:
            if alias in available:
                mapping[normalized] = available[alias]
                break
    return mapping


def inspect_csv_stream(path: Path, *, sample_rows: int = 10) -> dict[str, Any]:
    row_count = 0
    samples: list[dict[str, str]] = []
    symbols: set[str] = set()
    dates: list[str] = []
    normalized_dates: list[str] = []
    duplicates = 0
    invalid_ohlc = 0
    seen: set[tuple[str, str]] = set()
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise ValueError("CSV has no header")
        mapping = _normalized_headers(reader.fieldnames)
        header_row_present = True
        if len(mapping) < 7 and len(reader.fieldnames) == 7:
            handle.seek(0)
            reader = csv.DictReader(
                handle,
                fieldnames=["symbol", "date", "open", "high", "low", "close", "volume"],
            )
            mapping = _normalized_headers(reader.fieldnames or [])
            header_row_present = False
        for row in reader:
            row_count += 1
            if len(samples) < sample_rows:
                samples.append(dict(row))
            symbol = str(row.get(mapping.get("symbol", ""), "")).strip().upper()
            raw_date = str(row.get(mapping.get("trading_date", ""), "")).strip()
            if symbol:
                symbols.add(symbol)
            if raw_date:
                dates.append(raw_date)
                with suppress(ValueError):
                    normalized_dates.append(_parse_date(raw_date))
            if symbol and raw_date:
                key = (symbol, raw_date)
                duplicates += key in seen
                seen.add(key)
            try:
                values = {
                    name: Decimal(str(row[mapping[name]]).replace(",", ""))
                    for name in ("open", "high", "low", "close", "volume")
                }
                if not (
                    values["low"] <= min(values["open"], values["close"])
                    and values["high"] >= max(values["open"], values["close"])
                    and values["low"] >= 0
                    and values["volume"] >= 0
                ):
                    invalid_ohlc += 1
            except (InvalidOperation, KeyError, TypeError):
                invalid_ohlc += 1
    return {
        "headers": reader.fieldnames,
        "normalized_mapping": mapping,
        "header_row_present": header_row_present,
        "row_count": row_count,
        "sample": samples,
        "symbol_count": len(symbols),
        "symbol_sample": sorted(symbols)[:30],
        "raw_date_min": min(dates, default=None),
        "raw_date_max": max(dates, default=None),
        "normalized_date_min": min(normalized_dates, default=None),
        "normalized_date_max": max(normalized_dates, default=None),
        "duplicate_symbol_date_rows": duplicates,
        "invalid_ohlcv_rows": invalid_ohlc,
        "preview_only": True,
        "activated": False,
    }


def inspect_zip_csv(path: Path, *, sample_members: int = 20) -> dict[str, Any]:
    manifest = validate_archive_manifest(path)
    schemas: Counter[tuple[str, ...]] = Counter()
    inspected: list[dict[str, Any]] = []
    with zipfile.ZipFile(path) as archive:
        csv_infos = [item for item in archive.infolist() if item.filename.lower().endswith(".csv")]
        preferred = [
            item
            for item in csv_infos
            if any(
                marker in item.filename
                for marker in (
                    "/GP.csv",
                    "/ACI.csv",
                    "/BRACBANK.csv",
                    "/BEXIMCO.csv",
                    "company_metadata.csv",
                    "date_coverage_summary.csv",
                    "availability_matrix.csv",
                )
            )
        ]
        selected = (preferred + csv_infos)[:sample_members]
        selected_names: set[str] = set()
        for info in selected:
            if info.filename in selected_names:
                continue
            selected_names.add(info.filename)
            with archive.open(info) as handle:
                header = handle.readline().decode("utf-8-sig").strip()
                fields = next(csv.reader([header])) if header else []
            schemas[tuple(fields)] += 1
            inspected.append({"member": info.filename, "headers": fields, "bytes": info.file_size})
    return {
        **manifest,
        "schemas": [{"headers": list(key), "count": count} for key, count in schemas.items()],
        "inspected_members": inspected,
        "preview_only": True,
        "activated": False,
    }


def _parse_date(value: str) -> str:
    cleaned = value.strip()
    for format_string in ("%Y-%m-%d", "%d-%m-%Y", "%Y%m%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(cleaned, format_string).date().isoformat()
        except ValueError:
            continue
    raise ValueError(f"Unsupported trading date: {cleaned}")


def _csv_market_rows(
    path: Path, *, date_start: str | None = None, date_end: str | None = None
) -> dict[tuple[str, str], tuple[Decimal, ...]]:
    rows: dict[tuple[str, str], tuple[Decimal, ...]] = {}
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise ValueError("CSV has no header")
        mapping = _normalized_headers(reader.fieldnames)
        required = {"symbol", "trading_date", "open", "high", "low", "close", "volume"}
        if set(mapping) != required and len(reader.fieldnames) == 7:
            handle.seek(0)
            reader = csv.DictReader(
                handle,
                fieldnames=["symbol", "date", "open", "high", "low", "close", "volume"],
            )
            mapping = _normalized_headers(reader.fieldnames or [])
        if set(mapping) != required:
            raise ValueError(f"CSV mapping is incomplete: {sorted(required - set(mapping))}")
        for row in reader:
            try:
                key = (
                    str(row[mapping["symbol"]]).strip().upper(),
                    _parse_date(str(row[mapping["trading_date"]])),
                )
                if date_start and key[1] < date_start:
                    continue
                if date_end and key[1] > date_end:
                    continue
                values = tuple(
                    Decimal(str(row[mapping[field]]).replace(",", ""))
                    for field in ("open", "high", "low", "close", "volume")
                )
            except (InvalidOperation, KeyError, ValueError):
                continue
            if key in rows and rows[key] != values:
                raise ValueError(f"Conflicting duplicate source row: {key}")
            rows[key] = values
    return rows


def compare_csv_sources(
    left: Path,
    right: Path,
    *,
    left_label: str,
    right_label: str,
    output_dir: Path,
    tolerance: Decimal = Decimal("0.001"),
    date_start: str | None = None,
    date_end: str | None = None,
) -> dict[str, Any]:
    left_rows = _csv_market_rows(left, date_start=date_start, date_end=date_end)
    right_rows = _csv_market_rows(right, date_start=date_start, date_end=date_end)
    overlap = sorted(set(left_rows) & set(right_rows))
    ledger: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    for symbol, trading_date in overlap:
        left_values = left_rows[(symbol, trading_date)]
        right_values = right_rows[(symbol, trading_date)]
        differences = [
            abs(a - b) / max(abs(a), abs(b), Decimal("0.0001"))
            for a, b in zip(left_values, right_values, strict=True)
        ]
        if all(item == 0 for item in differences):
            classification = "exact_match"
        elif all(item <= tolerance for item in differences):
            classification = "within_tolerance"
        else:
            classification = "material_conflict"
        counts[classification] += 1
        if classification != "exact_match":
            ledger.append(
                {
                    "symbol": symbol,
                    "trading_date": trading_date,
                    "classification": classification,
                    "max_relative_difference": str(max(differences)),
                    "left_close": str(left_values[3]),
                    "right_close": str(right_values[3]),
                    "left_volume": str(left_values[4]),
                    "right_volume": str(right_values[4]),
                }
            )
    report = {
        "left_source": left_label,
        "right_source": right_label,
        "left_rows": len(left_rows),
        "right_rows": len(right_rows),
        "overlap_rows": len(overlap),
        "counts": dict(counts),
        "discrepancy_rows": len(ledger),
        "left_only_rows": len(set(left_rows) - set(right_rows)),
        "right_only_rows": len(set(right_rows) - set(left_rows)),
        "tolerance": str(tolerance),
        "date_start": date_start,
        "date_end": date_end,
        "prices_averaged": False,
        "human_review_required": bool(ledger),
        "ledger": ledger,
    }
    digest = canonical_hash(report)
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / f"discrepancy_{digest}.json"
    csv_path = output_dir / f"discrepancy_{digest}.csv"
    markdown_path = output_dir / f"discrepancy_{digest}.md"
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    fieldnames = [
        "symbol",
        "trading_date",
        "classification",
        "max_relative_difference",
        "left_close",
        "right_close",
        "left_volume",
        "right_volume",
    ]
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(ledger)
    markdown_path.write_text(
        "\n".join(
            (
                "# Public dataset discrepancy summary",
                "",
                f"- Left: {left_label}",
                f"- Right: {right_label}",
                f"- Overlapping symbol/date rows: {len(overlap)}",
                f"- Exact matches: {counts['exact_match']}",
                f"- Within tolerance: {counts['within_tolerance']}",
                f"- Material conflicts: {counts['material_conflict']}",
                f"- Human review required: {'yes' if ledger else 'no'}",
                "- No values were averaged, trusted, imported, or activated.",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    return {
        **report,
        "report_hash": digest,
        "output_paths": {
            "json": str(json_path),
            "csv": str(csv_path),
            "markdown": str(markdown_path),
        },
    }


def record_schema_preview(
    db: Session,
    dataset: GovernedDataset,
    inspection: dict[str, Any],
) -> DatasetImportRun:
    digest = canonical_hash({"dataset": dataset.raw_sha256, "inspection": inspection})
    run = DatasetImportRun(
        dataset_id=dataset.id,
        batch_hash=digest,
        column_mapping=inspection.get("normalized_mapping", {}),
        inferred_schema={
            "headers": inspection.get("headers"),
            "schemas": inspection.get("schemas"),
        },
        preview=inspection,
        state="review_required",
        row_count=int(inspection.get("row_count", 0)),
        errors=[{"classification": "activation_prohibited", "detail": "Human validation required"}],
    )
    db.add(run)
    db.flush()
    append_audit(
        db,
        actor=dataset.operator,
        event_type="research_dataset.schema_previewed",
        entity_type="dataset_import_run",
        entity_id=run.id,
        new_state={"preview_only": True, "activated": False, "batch_hash": digest},
    )
    db.commit()
    return run


def extract_rule_candidates(pages: list[str], *, max_per_topic: int = 5) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    seen: set[tuple[str, int, str]] = set()
    topic_counts: Counter[str] = Counter()
    for page_number, text in enumerate(pages, 1):
        collapsed = re.sub(r"\s+", " ", text).strip()
        lower = collapsed.lower()
        for topic, phrases in RULE_TOPICS.items():
            if topic_counts[topic] >= max_per_topic:
                continue
            for phrase in phrases:
                start = lower.find(phrase)
                if start < 0:
                    continue
                excerpt = collapsed[max(0, start - 140) : start + 360].strip()
                key = (topic, page_number, excerpt)
                if key not in seen:
                    candidates.append(
                        {
                            "claim_type": topic,
                            "source_location": f"page {page_number}",
                            "original_value": excerpt,
                            "normalized_interpretation": {
                                "unverified": True,
                                "requires_legal_and_operator_review": True,
                            },
                        }
                    )
                    seen.add(key)
                    topic_counts[topic] += 1
                break
    return candidates


def register_under_review_claims(
    db: Session,
    evidence: AuthoritativeEvidence,
    candidates: list[dict[str, Any]],
    *,
    source_url: str,
    actor: str,
) -> list[ExtractedClaim]:
    claims: list[ExtractedClaim] = []
    for item in candidates:
        claim = ExtractedClaim(
            evidence_id=evidence.id,
            claim_type=str(item["claim_type"]),
            source_location=str(item["source_location"]),
            original_value=str(item["original_value"]),
            normalized_interpretation={
                **dict(item["normalized_interpretation"]),
                "source_url": source_url,
                "source_file": evidence.original_filename,
            },
            confidence="unknown",
            extraction_method="deterministic_pdf_text",
            reviewer_status="under_review",
        )
        db.add(claim)
        claims.append(claim)
    db.flush()
    event = append_audit(
        db,
        actor=actor,
        event_type="public_rule_evidence.extracted",
        entity_type="authoritative_evidence",
        entity_id=evidence.id,
        new_state={"claim_count": len(claims), "status": "under_review", "activated": False},
    )
    for claim in claims:
        claim.audit_event_ids = [event.id]
    evidence.verification_status = "under_review"
    evidence.extraction = {
        **evidence.extraction,
        "claim_count": len(claims),
        "human_verified": False,
        "source_url": source_url,
    }
    db.commit()
    return claims


def write_attempt_manifest(attempts: list[SourceAttempt], path: Path) -> str:
    if len({(item.source_url, item.local_path) for item in attempts}) != len(attempts):
        raise ValueError("Every source attempt must be represented exactly once")
    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "attempts": [asdict(item) for item in attempts],
        "qualification": "0/60",
        "automatic_activation": False,
    }
    digest = str(canonical_hash(payload))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({**payload, "manifest_hash": digest}, indent=2), encoding="utf-8")
    return digest
