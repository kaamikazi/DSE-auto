from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from datetime import time as datetime_time
from decimal import ROUND_DOWN, Decimal
from html.parser import HTMLParser
from pathlib import Path
from statistics import mean
from typing import Any, BinaryIO, cast
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.brokers.paper import PaperBroker
from app.core.config import Settings, assert_paper_only_safety, get_settings
from app.core.database import database_health_metadata
from app.core.database_identity import REPOSITORY_ROOT
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
from app.services.absolute_momentum_filter import (
    PRIMARY_CONFIG,
    PRIMARY_PARAMETERS,
    STRATEGY_IDENTITY,
    absolute_momentum_scores,
    deterministic_registration_id,
)
from app.services.audit import append_audit, verify_audit_chain
from app.services.expanded_strategy_validation import (
    EXPANDED_UNIVERSE,
    LoadedUniverse,
    load_expanded_universe,
    validate_frozen_identities,
)
from app.services.paper_sessions import create_session, transition_session

SESSION_NAME = "minimal-v1-absolute-momentum-forward-validation"
ACCOUNT_LABEL = "minimal_v1_forward_absolute_momentum"
DEFAULT_STARTING_CASH = Decimal("1000000.00")
FEE_RATE = Decimal("0.004")
SLIPPAGE_RATE = Decimal("0.0025")
LOCK_RELATIVE_PATH = Path("data/process-state/minimal_v1_forward.lock")
ALERT_RELATIVE_PATH = Path("logs/minimal_v1_forward_alerts.log")
MAX_ALERT_BYTES = 1_000_000
TRUSTED_TIMESTAMPS = {
    TimestampProvenance.EXCHANGE_VERIFIED,
    TimestampProvenance.OPERATOR_ATTESTED,
}
MANUAL_SOURCE_IDENTITY = "official_dse_public_eod_archive"
MANUAL_ATTESTATION = (
    "I manually obtained this official DSE public EOD/archive file, the stated market session "
    "had completed, and these observations were visible when I acquired it."
)
MANUAL_PARSER_VERSION = "dse_public_eod_manual_v2"
MANUAL_EVIDENCE_CLASS = "FORWARD_OPERATOR_ATTESTED"
MANUAL_INGEST_RUN_TYPE = "manual_forward_ingest"
MANUAL_INGEST_RELATIVE_PATH = Path("data/process-state/minimal_v1_forward")
MANUAL_FILE_SUFFIXES = {".csv", ".htm", ".html"}
MAX_MANUAL_FILE_BYTES = 100 * 1024 * 1024
FORWARD_INGEST_BOUNDARY_COMMIT = "e64b0a8f2bc211eaed46c2f1bd739e01970363bf"
FORWARD_INGEST_BOUNDARY_AT = datetime.fromisoformat("2026-08-10T12:16:02+06:00")
_DSE_SYMBOL = re.compile(r"^[A-Z0-9().&_-]{1,32}$")
_DSE_REQUIRED_COLUMNS = {
    "date": "DATE",
    "symbol": "TRADING CODE",
    "open": "OPENP*",
    "high": "HIGH",
    "low": "LOW",
    "close": "CLOSEP*",
    "volume": "VOLUME",
}


class ForwardValidationError(RuntimeError):
    """A fail-closed forward-validation boundary violation."""


class _DSEHTMLTableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[str]]] = []
        self._table: list[list[str]] | None = None
        self._row: list[str] | None = None
        self._cell: list[str] | None = None
        self._cell_fallback: str | None = None

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        lowered = tag.lower()
        if lowered == "table" and self._table is None:
            self._table = []
        elif lowered == "tr" and self._table is not None:
            self._row = []
        elif lowered in {"td", "th"} and self._row is not None:
            self._cell = []
            self._cell_fallback = dict(attrs).get("aria-label") if lowered == "th" else None

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.lower()
        if lowered in {"td", "th"} and self._cell is not None and self._row is not None:
            content = " ".join("".join(self._cell).split())
            fallback = " ".join((self._cell_fallback or "").split())
            self._row.append(content or fallback)
            self._cell = None
            self._cell_fallback = None
        elif lowered == "tr" and self._row is not None and self._table is not None:
            if self._row:
                self._table.append(self._row)
            self._row = None
        elif lowered == "table" and self._table is not None:
            self.tables.append(self._table)
            self._table = None


def _canonical_json_bytes(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), default=str) + "\n").encode()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_immutable(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        if path.read_bytes() != payload:
            raise ForwardValidationError(f"Immutable evidence collision: {path}") from None


def _candidate_tables(raw: bytes, suffix: str) -> list[list[list[str]]]:
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ForwardValidationError("Manual DSE file must be UTF-8 text") from exc
    if suffix == ".csv":
        rows = [[cell.strip() for cell in row] for row in csv.reader(io.StringIO(text))]
        if not rows:
            raise ForwardValidationError("Manual DSE CSV contains no rows")
        return [rows]
    parser = _DSEHTMLTableParser()
    parser.feed(text)
    candidates = [
        table
        for table in parser.tables
        if any(set(_DSE_REQUIRED_COLUMNS.values()).issubset(set(row)) for row in table)
    ]
    if not candidates:
        raise ForwardValidationError("Official DSE EOD table was not found in the HTML file")
    return candidates


def _decimal_cell(value: str, *, field: str, symbol: str) -> Decimal:
    try:
        parsed = Decimal(value.replace(",", ""))
    except ArithmeticError as exc:
        raise ForwardValidationError(f"Invalid {field} for {symbol}") from exc
    if not parsed.is_finite() or parsed < 0:
        raise ForwardValidationError(f"Invalid {field} for {symbol}")
    return parsed


def _volume_cell(value: str, *, symbol: str) -> int:
    try:
        parsed = int(value.replace(",", ""))
    except ValueError as exc:
        raise ForwardValidationError(f"Invalid volume for {symbol}") from exc
    if parsed < 0:
        raise ForwardValidationError(f"Invalid volume for {symbol}")
    return parsed


def _decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _jsonable(value: Any) -> Any:
    return json.loads(json.dumps(value, sort_keys=True, default=str))


def _sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _bar_payload(bar: HistoricalBar) -> dict[str, Any]:
    return {
        "timestamp": bar.timestamp.isoformat(),
        "symbol": bar.symbol,
        "open": str(bar.open),
        "high": str(bar.high),
        "low": str(bar.low),
        "close": str(bar.close),
        "volume": bar.volume,
        "source": bar.source,
        "timestamp_provenance": str(bar.timestamp_provenance),
        "quality_flags": sorted(bar.quality_flags),
    }


def quarter_end_sessions(sessions: Sequence[date]) -> set[date]:
    endings: dict[tuple[int, int], date] = {}
    for day in sorted(set(sessions)):
        endings[(day.year, (day.month - 1) // 3 + 1)] = day
    return set(endings.values())


class RunnerLock:
    """Non-blocking local OS lock with a readable owner record."""

    def __init__(self, path: Path, identity: Mapping[str, Any]) -> None:
        self.path = path
        self.identity = dict(identity)
        self._handle: BinaryIO | None = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        if handle.seek(0, os.SEEK_END) == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(  # type: ignore[attr-defined]
                    handle.fileno(),
                    fcntl.LOCK_EX | fcntl.LOCK_NB,  # type: ignore[attr-defined]
                )
        except OSError as exc:
            handle.close()
            raise ForwardValidationError(
                f"Forward runner already active: {json.dumps(self.existing_owner(), sort_keys=True)}"
            ) from exc
        owner = {
            **self.identity,
            "pid": os.getpid(),
            "acquired_at": datetime.now(UTC).isoformat(),
        }
        handle.seek(0)
        handle.truncate()
        handle.write(b"\0" + json.dumps(owner, sort_keys=True).encode())
        handle.flush()
        self._handle = handle

    def existing_owner(self) -> dict[str, Any]:
        try:
            with self.path.open("rb") as handle:
                handle.seek(1)
                raw = handle.read().decode("utf-8").strip()
            return cast(dict[str, Any], json.loads(raw)) if raw else {}
        except (OSError, json.JSONDecodeError):
            return {"identity": "unreadable_lock_owner"}

    def release(self) -> None:
        if self._handle is None:
            return
        self._handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(  # type: ignore[attr-defined]
                self._handle.fileno(),
                fcntl.LOCK_UN,  # type: ignore[attr-defined]
            )
        self._handle.close()
        self._handle = None

    def __enter__(self) -> RunnerLock:
        self.acquire()
        return self

    def __exit__(self, *_: object) -> None:
        self.release()


class ForwardPaperValidationRunner:
    def __init__(
        self,
        db: Session,
        *,
        repository_root: Path = REPOSITORY_ROOT,
        settings: Settings | None = None,
        implementation_boundary: datetime | None = None,
    ) -> None:
        self.db = db
        self.repository_root = repository_root
        self.settings = settings or get_settings()
        self._implementation_boundary_override = implementation_boundary

    @property
    def lock(self) -> RunnerLock:
        return RunnerLock(
            self.repository_root / LOCK_RELATIVE_PATH,
            {"runner": SESSION_NAME, "strategy": STRATEGY_IDENTITY},
        )

    @staticmethod
    def _dataset_snapshot(loaded: LoadedUniverse) -> list[dict[str, Any]]:
        return [
            {
                "id": item["registry_id"],
                "sha256": item["dataset_sha256"],
                "source_sha256": item["source_sha256"],
                "symbols": item["symbols"],
            }
            for item in loaded.datasets
        ]

    def verify_frozen_contract(self) -> tuple[dict[str, Any], LoadedUniverse]:
        identities = validate_frozen_identities(self.db, self.repository_root)
        identity = next(
            (item for item in identities if item["identity"] == STRATEGY_IDENTITY), None
        )
        if identity is None:
            raise ForwardValidationError("Frozen absolute-momentum registration is unavailable")
        loaded = load_expanded_universe(self.db, self.repository_root)
        datasets = self._dataset_snapshot(loaded)
        registration = self.db.scalar(
            select(StrategyRegistration).where(
                StrategyRegistration.strategy_id == "absolute_momentum_filter",
                StrategyRegistration.version == "0.1.0",
            )
        )
        if registration is None:
            raise ForwardValidationError("Frozen strategy registration record is unavailable")
        registered_datasets = registration.data_requirements.get("active_dataset_ids_and_hashes")
        if not isinstance(registered_datasets, list) or not registered_datasets:
            raise ForwardValidationError("Frozen registration dataset binding is malformed")
        expected_registration = deterministic_registration_id(
            code_sha256=str(identity["code_sha256"]),
            parameter_sha256=str(identity["parameter_sha256"]),
            datasets=registered_datasets,
        )
        if identity["registration_id"] != expected_registration:
            raise ForwardValidationError("Frozen strategy registration ID mismatch")
        active_pairs = {(item["id"], item["sha256"]) for item in datasets}
        registered_pairs = {
            (str(item.get("id")), str(item.get("sha256")))
            for item in registered_datasets
            if isinstance(item, dict)
        }
        if len(datasets) != 4 or not registered_pairs.issubset(active_pairs):
            raise ForwardValidationError(
                "Expanded active datasets do not preserve registration lineage"
            )
        timing = {
            "rebalance_frequency": PRIMARY_PARAMETERS["rebalance_frequency"],
            "execution": PRIMARY_PARAMETERS["execution"],
            "same_bar_execution": False,
        }
        costs = {
            "fee_percent": PRIMARY_PARAMETERS["fee_percent"],
            "slippage_percent": PRIMARY_PARAMETERS["slippage_percent"],
        }
        if timing != {
            "rebalance_frequency": "quarterly",
            "execution": "next_common_source_present_open",
            "same_bar_execution": False,
        } or costs != {"fee_percent": "0.40", "slippage_percent": "0.25"}:
            raise ForwardValidationError("Frozen timing or cost contract mismatch")
        return (
            {
                **identity,
                "implementation": "app.services.absolute_momentum_filter",
                "timing_contract": timing,
                "cost_contract": costs,
                "datasets": datasets,
            },
            loaded,
        )

    def verify_startup(self) -> tuple[dict[str, Any], LoadedUniverse]:
        assert_paper_only_safety(self.settings)
        database = database_health_metadata(self.db)
        if not database["healthy"] or self.settings.DATABASE_ROLE not in {"operational", "test"}:
            raise ForwardValidationError("Database identity or health check failed")
        if not verify_audit_chain(self.db):
            raise ForwardValidationError("Canonical audit chain is invalid")
        risk = self.db.get(RiskState, 1)
        if risk is None or risk.state in {"emergency_stop", "reconciliation_required"}:
            raise ForwardValidationError("Global emergency/reconciliation state blocks startup")
        now = datetime.now(UTC)
        local = now.astimezone(ZoneInfo("Asia/Dhaka"))
        if not 2025 <= now.year <= 2100 or local.utcoffset() is None:
            raise ForwardValidationError("Local clock sanity check failed")
        identity, loaded = self.verify_frozen_contract()
        return (
            {
                "paper_only": True,
                "database": database,
                "audit_valid": True,
                "clock_utc": now.isoformat(),
                "clock_dhaka": local.isoformat(),
                "strategy": identity,
            },
            loaded,
        )

    def provider_readiness(self) -> dict[str, Any]:
        provider = self.settings.DATA_PRIMARY_PROVIDER.lower()
        blockers: list[str] = []
        if provider in {"mock", "fake_certified"}:
            blockers.append("synthetic_provider_forbidden_for_forward_validation")
        if provider != "attested_csv":
            blockers.append("trusted_adjusted_eod_provider_not_configured")
        blockers.append("no_certified_forward_ingestion_contract")
        calendar_path = self.repository_root / "config/dse_market_calendar.yaml"
        holidays_path = self.repository_root / "data/imports/dse_holidays.csv"
        try:
            calendar = json.loads(calendar_path.read_text(encoding="utf-8"))
            if calendar.get("operator_verified") is not True:
                blockers.append("market_calendar_not_operator_verified")
        except (OSError, json.JSONDecodeError):
            blockers.append("market_calendar_unavailable")
        if not holidays_path.is_file():
            blockers.append("holiday_calendar_unavailable")
        return {
            "ready": not blockers,
            "provider": provider,
            "automated_provider_certified": False,
            "required_timestamp_trust": ["exchange_verified", "operator_attested"],
            "required_adjustment_grain": "adjusted",
            "required_lineage": "validated",
            "blockers": blockers,
        }

    def _git(self, *arguments: str) -> str:
        try:
            completed = subprocess.run(
                ["git", *arguments],
                cwd=self.repository_root,
                check=True,
                capture_output=True,
                text=True,
                timeout=15,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ForwardValidationError(
                "Committed implementation boundary is unavailable"
            ) from exc
        return completed.stdout.strip()

    def _implementation_boundary(self) -> dict[str, str]:
        if self._implementation_boundary_override is not None:
            boundary = self._implementation_boundary_override
            if boundary.tzinfo is None or boundary.utcoffset() is None:
                raise ForwardValidationError("Implementation boundary must include a UTC offset")
            return {
                "commit": "injected_test_boundary",
                "committed_at": boundary.astimezone(UTC).isoformat(),
            }
        if self._git("status", "--porcelain", "--untracked-files=no"):
            raise ForwardValidationError(
                "forward-ingest requires the implementation to be committed with no tracked changes"
            )
        head = self._git("rev-parse", "HEAD")
        required_markers = {
            "backend/app/minimal_v1_cli.py": "forward-ingest",
            "backend/app/services/forward_paper_validation.py": "def ingest_manual_eod",
        }
        for tracked_path, marker in required_markers.items():
            committed = self._git("show", f"{head}:{tracked_path}")
            if marker not in committed:
                raise ForwardValidationError("forward-ingest implementation is not committed")
        self._git("merge-base", "--is-ancestor", FORWARD_INGEST_BOUNDARY_COMMIT, head)
        committed_at = datetime.fromisoformat(
            self._git("show", "-s", "--format=%cI", FORWARD_INGEST_BOUNDARY_COMMIT)
        )
        if committed_at.tzinfo is None or committed_at.utcoffset() is None:
            raise ForwardValidationError("Git commit timestamp is not timezone-aware")
        if committed_at != FORWARD_INGEST_BOUNDARY_AT:
            raise ForwardValidationError("Committed implementation boundary identity changed")
        return {
            "commit": FORWARD_INGEST_BOUNDARY_COMMIT,
            "committed_at": committed_at.astimezone(UTC).isoformat(),
        }

    @staticmethod
    def _parse_manual_table(
        rows: list[list[str]], claimed_market_date: date
    ) -> dict[str, Any] | None:
        header_index = next(
            (
                index
                for index, row in enumerate(rows)
                if set(_DSE_REQUIRED_COLUMNS.values()).issubset(set(row))
            ),
            None,
        )
        if header_index is None:
            raise ForwardValidationError("Official DSE EOD columns are missing")
        header = rows[header_index]
        if len(header) != len(set(header)):
            raise ForwardValidationError("Official DSE EOD header contains duplicate columns")
        positions = {name: header.index(column) for name, column in _DSE_REQUIRED_COLUMNS.items()}
        parsed: dict[str, dict[str, Any]] = {}
        unavailable: dict[str, str] = {}
        symbol_set: set[str] = set()
        source_row_count = 0
        for row_number, row in enumerate(rows[header_index + 1 :], header_index + 2):
            if not any(cell.strip() for cell in row):
                continue
            if len(row) < len(header):
                raise ForwardValidationError(f"Malformed DSE row {row_number}")
            try:
                row_date = date.fromisoformat(row[positions["date"]].strip())
            except ValueError as exc:
                raise ForwardValidationError(f"Invalid market date on row {row_number}") from exc
            if row_date != claimed_market_date:
                raise ForwardValidationError(
                    f"Claimed-date mismatch on row {row_number}: {row_date.isoformat()}"
                )
            symbol = row[positions["symbol"]].strip().upper()
            if not _DSE_SYMBOL.fullmatch(symbol):
                raise ForwardValidationError(f"Invalid DSE symbol on row {row_number}")
            if symbol in symbol_set:
                raise ForwardValidationError(f"Duplicate DSE symbol for claimed date: {symbol}")
            symbol_set.add(symbol)
            source_row_count += 1
            open_price = _decimal_cell(row[positions["open"]], field="open", symbol=symbol)
            high = _decimal_cell(row[positions["high"]], field="high", symbol=symbol)
            low = _decimal_cell(row[positions["low"]], field="low", symbol=symbol)
            close = _decimal_cell(row[positions["close"]], field="close", symbol=symbol)
            volume = _volume_cell(row[positions["volume"]], symbol=symbol)
            if symbol not in EXPANDED_UNIVERSE:
                continue
            if min(open_price, high, low, close) == 0:
                unavailable[symbol] = "nonpositive_source_price_not_synthesized"
                continue
            if not low <= min(open_price, close) <= max(open_price, close) <= high:
                raise ForwardValidationError(f"Malformed OHLC for {symbol}")
            parsed[symbol] = {
                "symbol": symbol,
                "market_date": claimed_market_date.isoformat(),
                "open": _decimal_text(open_price),
                "high": _decimal_text(high),
                "low": _decimal_text(low),
                "close": _decimal_text(close),
                "volume": volume,
            }
        if source_row_count == 0:
            return None
        if not parsed:
            raise ForwardValidationError("Official DSE EOD file contains no eligible universe rows")
        missing = sorted(set(EXPANDED_UNIVERSE) - set(parsed))
        for symbol in missing:
            unavailable.setdefault(symbol, "source_row_absent")
        observations = [parsed[symbol] for symbol in EXPANDED_UNIVERSE if symbol in parsed]
        return {
            "source_row_count": source_row_count,
            "source_symbol_set": sorted(symbol_set),
            "observations": observations,
            "eligible_symbols": [row["symbol"] for row in observations],
            "missing_symbols": missing,
            "unavailable_symbols": unavailable,
        }

    @staticmethod
    def _parse_manual_rows(raw: bytes, suffix: str, claimed_market_date: date) -> dict[str, Any]:
        populated = [
            parsed
            for rows in _candidate_tables(raw, suffix)
            if (
                parsed := ForwardPaperValidationRunner._parse_manual_table(
                    rows, claimed_market_date
                )
            )
            is not None
        ]
        if not populated:
            raise ForwardValidationError("Official DSE EOD file contains no observations")
        if len(populated) != 1:
            raise ForwardValidationError(
                "Official DSE EOD HTML contains ambiguous populated matching tables"
            )
        return populated[0]

    def _manual_runs(self, session: PaperSession) -> list[PaperSessionRun]:
        return self._runs(session, MANUAL_INGEST_RUN_TYPE)

    def _ensure_manual_audit(self, event_id: str, metrics: Mapping[str, Any]) -> None:
        existing = self.db.scalar(
            select(AuditEvent).where(
                AuditEvent.event_type == "data_import.activated",
                AuditEvent.entity_type == "paper_session_run",
                AuditEvent.entity_id == event_id,
            )
        )
        if existing is not None:
            return
        append_audit(
            self.db,
            actor="operator",
            event_type="data_import.activated",
            entity_type="paper_session_run",
            entity_id=event_id,
            new_state={
                "evidence_class": MANUAL_EVIDENCE_CLASS,
                "market_date": metrics["market_date"],
                "raw_sha256": metrics["raw_sha256"],
                "normalized_sha256": metrics["normalized_sha256"],
                "row_count": metrics["source_row_count"],
                "trading_effect": False,
            },
            metadata={
                "source_identity": metrics["source_identity"],
                "operator_attestation": metrics["operator_attestation"],
                "receipt_timestamp": metrics["receipt_timestamp"],
                "timestamp_provenance": "operator_attested",
                "exchange_verified": False,
                "automated_provider_certified": False,
            },
        )

    def ingest_manual_eod(
        self,
        input_path: Path,
        claimed_market_date: date,
        source_identity: str,
        operator_attestation: str,
        session_completed_at: datetime,
        *,
        receipt_time: datetime | None = None,
    ) -> dict[str, Any]:
        assert_paper_only_safety(self.settings)
        if source_identity != MANUAL_SOURCE_IDENTITY:
            raise ForwardValidationError(
                f"Manual source identity must be exactly {MANUAL_SOURCE_IDENTITY}"
            )
        if operator_attestation != MANUAL_ATTESTATION:
            raise ForwardValidationError(f"Operator must attest exactly: {MANUAL_ATTESTATION}")
        path_text = str(input_path)
        if (
            "://" in path_text
            or re.match(r"^(?:https?|ftp):[\\/]", path_text, re.IGNORECASE)
            or path_text.startswith(("\\\\", "//"))
        ):
            raise ForwardValidationError("forward-ingest accepts only a local file path")
        if input_path.is_symlink():
            raise ForwardValidationError("forward-ingest refuses symbolic-link input")
        try:
            resolved = input_path.resolve(strict=True)
        except OSError as exc:
            raise ForwardValidationError("Manual DSE input file does not exist") from exc
        if not resolved.is_file():
            raise ForwardValidationError("Manual DSE input must be a regular local file")
        suffix = resolved.suffix.lower()
        if suffix not in MANUAL_FILE_SUFFIXES:
            raise ForwardValidationError("Manual DSE input must be CSV or HTML")
        size = resolved.stat().st_size
        if size <= 0 or size > MAX_MANUAL_FILE_BYTES:
            raise ForwardValidationError("Manual DSE input file size is invalid")
        raw = resolved.read_bytes()
        raw_sha256 = hashlib.sha256(raw).hexdigest()
        receipt = receipt_time or datetime.now(UTC)
        if receipt.tzinfo is None or receipt.utcoffset() is None:
            raise ForwardValidationError("Local receipt timestamp must include a UTC offset")
        receipt = receipt.astimezone(UTC)
        startup, _ = self.verify_startup()
        session = self._session()
        if session is None:
            raise ForwardValidationError(
                "Existing authorized Minimal V1 forward session is required"
            )
        identity = cast(dict[str, Any], startup["strategy"])
        self.ensure_session(identity, starting_cash=DEFAULT_STARTING_CASH)
        same_hash = next(
            (
                run
                for run in self._manual_runs(session)
                if run.metrics.get("raw_sha256") == raw_sha256
            ),
            None,
        )
        if same_hash is not None:
            if same_hash.metrics.get("market_date") != claimed_market_date.isoformat():
                raise ForwardValidationError("Raw snapshot was previously bound to another date")
            self._load_manual_observation(same_hash, now=receipt)
            self._ensure_manual_audit(str(same_hash.metrics["event_id"]), same_hash.metrics)
            return same_hash.metrics
        boundary = self._implementation_boundary()
        boundary_time = datetime.fromisoformat(boundary["committed_at"])
        if session_completed_at.tzinfo is None or session_completed_at.utcoffset() is None:
            raise ForwardValidationError("Attested session completion must include a UTC offset")
        completed_dhaka = session_completed_at.astimezone(ZoneInfo("Asia/Dhaka"))
        if completed_dhaka.date() != claimed_market_date:
            raise ForwardValidationError("Attested session completion does not match market date")
        if session_completed_at <= boundary_time:
            raise ForwardValidationError(
                "Forward evidence cannot predate the committed implementation boundary"
            )
        if session_completed_at > receipt:
            raise ForwardValidationError(
                "Attested session completion cannot be after local receipt"
            )
        parsed = self._parse_manual_rows(raw, suffix, claimed_market_date)
        normalized = {
            "schema": "minimal_v1_manual_eod_v1",
            "parser_version": MANUAL_PARSER_VERSION,
            "evidence_class": MANUAL_EVIDENCE_CLASS,
            "source_identity": source_identity,
            "market_date": claimed_market_date.isoformat(),
            "adjustment_grain": "raw_unadjusted",
            "observations": parsed["observations"],
        }
        normalized_bytes = _canonical_json_bytes(normalized)
        normalized_sha256 = hashlib.sha256(normalized_bytes).hexdigest()
        prior_versions = [
            run
            for run in self._manual_runs(session)
            if run.metrics.get("market_date") == claimed_market_date.isoformat()
        ]
        version = len(prior_versions) + 1
        supersedes = str(prior_versions[-1].metrics["event_id"]) if prior_versions else None
        event_id = _sha256(
            {
                "manual_forward_ingest": session.id,
                "market_date": claimed_market_date,
                "source_identity": source_identity,
                "raw_sha256": raw_sha256,
                "normalized_sha256": normalized_sha256,
            }
        )
        evidence_dir = (
            self.repository_root
            / MANUAL_INGEST_RELATIVE_PATH
            / session.id
            / "manual_eod"
            / claimed_market_date.isoformat()
            / raw_sha256
        )
        raw_path = evidence_dir / f"raw_snapshot{suffix}"
        normalized_path = evidence_dir / "normalized.json"
        evidence_path = evidence_dir / "evidence.json"
        relative_raw = raw_path.relative_to(self.repository_root).as_posix()
        relative_normalized = normalized_path.relative_to(self.repository_root).as_posix()
        relative_evidence = evidence_path.relative_to(self.repository_root).as_posix()
        availability = max(session_completed_at.astimezone(UTC), receipt)
        evidence = {
            "schema": "minimal_v1_manual_eod_evidence_v1",
            "event_id": event_id,
            "evidence_class": MANUAL_EVIDENCE_CLASS,
            "market_date": claimed_market_date.isoformat(),
            "version": version,
            "supersedes_event_id": supersedes,
            "source_identity": source_identity,
            "original_filename": resolved.name,
            "raw_sha256": raw_sha256,
            "raw_byte_count": len(raw),
            "raw_snapshot_relative_path": relative_raw,
            "normalized_sha256": normalized_sha256,
            "normalized_relative_path": relative_normalized,
            "parser_version": MANUAL_PARSER_VERSION,
            "operator_attestation": operator_attestation,
            "session_completed_at": session_completed_at.astimezone(UTC).isoformat(),
            "receipt_timestamp": receipt.isoformat(),
            "availability_timestamp": availability.isoformat(),
            "timestamp_semantics": "local receipt; not DSE publication time",
            "timestamp_provenance": "operator_attested",
            "adjustment_grain": "raw_unadjusted",
            "analytical_adjustment_status": "unresolved",
            "source_row_count": parsed["source_row_count"],
            "source_symbol_set": parsed["source_symbol_set"],
            "eligible_symbols": parsed["eligible_symbols"],
            "missing_symbols": parsed["missing_symbols"],
            "unavailable_symbols": parsed["unavailable_symbols"],
            "implementation_boundary": boundary,
            "automated_provider_certified": False,
            "trading_effect": False,
        }
        evidence_bytes = _canonical_json_bytes(evidence)
        _write_immutable(raw_path, raw)
        _write_immutable(normalized_path, normalized_bytes)
        _write_immutable(evidence_path, evidence_bytes)
        metrics = {
            **evidence,
            "evidence_relative_path": relative_evidence,
            "status": "accepted" if not parsed["missing_symbols"] else "accepted_with_missing",
            "runner_visible": True,
            "decision_eligible": False,
            "decision_blockers": [
                "raw_unadjusted_forward_observation",
                "analytical_adjustment_view_unresolved",
                "period_end_calendar_evidence_unresolved",
            ],
        }
        run = self._record(
            session,
            MANUAL_INGEST_RUN_TYPE,
            event_id,
            status="completed",
            metrics=metrics,
        )
        self._ensure_manual_audit(event_id, run.metrics)
        return run.metrics

    def _session(self) -> PaperSession | None:
        return self.db.scalar(select(PaperSession).where(PaperSession.name == SESSION_NAME))

    def ensure_session(
        self, identity: Mapping[str, Any], *, starting_cash: Decimal
    ) -> PaperSession:
        session = self._session()
        if session is not None:
            account = self.db.get(PaperAccount, session.account_id)
            expected = cast(Mapping[str, Any], session.risk_profile).get("frozen_identity")
            if (
                account is None
                or account.starting_cash != starting_cash
                or session.strategies != [STRATEGY_IDENTITY]
                or session.approved_universe != sorted(EXPANDED_UNIVERSE)
                or expected != _jsonable(identity)
            ):
                raise ForwardValidationError("Dedicated forward session identity changed")
            return session
        account_id = int(self.db.scalar(select(func.max(PaperAccount.id))) or 0) + 1
        self.db.add(
            PaperAccount(
                id=account_id,
                cash=starting_cash,
                starting_cash=starting_cash,
                active=True,
                as_of=date.today(),
            )
        )
        self.db.commit()
        return create_session(
            self.db,
            SESSION_NAME,
            list(EXPANDED_UNIVERSE),
            [STRATEGY_IDENTITY],
            {
                "purpose": "forward_paper_validation_only",
                "qualification": "0/60",
                "account_label": ACCOUNT_LABEL,
                "frozen_identity": _jsonable(identity),
                "forward_performance_excludes_replay": True,
            },
            fill_model="pessimistic",
            account_id=account_id,
        )

    def _runs(self, session: PaperSession, run_type: str | None = None) -> list[PaperSessionRun]:
        query = select(PaperSessionRun).where(PaperSessionRun.session_id == session.id)
        if run_type is not None:
            query = query.where(PaperSessionRun.run_type == run_type)
        return list(self.db.scalars(query.order_by(PaperSessionRun.id)))

    def _run_by_event(self, session: PaperSession, event_id: str) -> PaperSessionRun | None:
        return next(
            (
                item
                for item in self._runs(session)
                if (item.metrics or {}).get("event_id") == event_id
            ),
            None,
        )

    def _ledger_path(self, session: PaperSession) -> Path:
        return (
            self.repository_root
            / "data/process-state/minimal_v1_forward"
            / session.id
            / "ledger.jsonl"
        )

    def _ensure_ledger_line(self, session: PaperSession, run: PaperSessionRun) -> None:
        path = self._ledger_path(session)
        path.parent.mkdir(parents=True, exist_ok=True)
        event_id = str(run.metrics["event_id"])
        if path.exists():
            with path.open(encoding="utf-8") as handle:
                if any(f'"event_id": "{event_id}"' in line for line in handle):
                    return
        line = {
            "schema": "minimal_v1_forward_ledger_v1",
            "record_id": run.id,
            "run_type": run.run_type,
            "status": run.status,
            "reason": run.reason,
            "recorded_at": run.started_at.isoformat(),
            **run.metrics,
        }
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(line, sort_keys=True, default=str) + "\n")

    def _record(
        self,
        session: PaperSession,
        run_type: str,
        event_id: str,
        *,
        status: str = "completed",
        reason: str | None = None,
        metrics: Mapping[str, Any] | None = None,
    ) -> PaperSessionRun:
        existing = self._run_by_event(session, event_id)
        if existing is not None:
            self._ensure_ledger_line(session, existing)
            return existing
        now = datetime.now(UTC)
        run = PaperSessionRun(
            session_id=session.id,
            run_type=run_type,
            status=status,
            reason=reason,
            metrics=_jsonable({"event_id": event_id, **dict(metrics or {})}),
            started_at=now,
            finished_at=now,
        )
        self.db.add(run)
        self.db.commit()
        self.db.refresh(run)
        self._ensure_ledger_line(session, run)
        return run

    def _resolved_evidence_path(self, relative_path: object) -> Path:
        root = self.repository_root.resolve()
        target = (root / str(relative_path)).resolve()
        if not target.is_relative_to(root):
            raise ForwardValidationError("Persisted manual evidence path escapes repository root")
        return target

    def _load_manual_observation(
        self,
        run: PaperSessionRun,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        metrics = run.metrics
        if metrics.get("evidence_class") != MANUAL_EVIDENCE_CLASS:
            raise ForwardValidationError("Manual observation evidence class is invalid")
        if metrics.get("adjustment_grain") != "raw_unadjusted":
            raise ForwardValidationError("Manual DSE observations must remain raw/unadjusted")
        raw_path = self._resolved_evidence_path(metrics.get("raw_snapshot_relative_path"))
        normalized_path = self._resolved_evidence_path(metrics.get("normalized_relative_path"))
        evidence_path = self._resolved_evidence_path(metrics.get("evidence_relative_path"))
        for path in (raw_path, normalized_path, evidence_path):
            if not path.is_file():
                raise ForwardValidationError(f"Manual evidence file is missing: {path.name}")
        if _file_sha256(raw_path) != metrics.get("raw_sha256"):
            raise ForwardValidationError("Immutable raw snapshot hash mismatch")
        normalized_bytes = normalized_path.read_bytes()
        if hashlib.sha256(normalized_bytes).hexdigest() != metrics.get("normalized_sha256"):
            raise ForwardValidationError("Normalized manual payload hash mismatch")
        try:
            normalized = cast(dict[str, Any], json.loads(normalized_bytes))
            evidence = cast(dict[str, Any], json.loads(evidence_path.read_bytes()))
        except json.JSONDecodeError as exc:
            raise ForwardValidationError("Manual evidence JSON is malformed") from exc
        for key in (
            "event_id",
            "market_date",
            "raw_sha256",
            "normalized_sha256",
            "availability_timestamp",
        ):
            if evidence.get(key) != metrics.get(key):
                raise ForwardValidationError(f"Manual evidence identity mismatch: {key}")
        if normalized.get("adjustment_grain") != "raw_unadjusted":
            raise ForwardValidationError("Normalized manual payload mislabeled adjustment grain")
        availability = datetime.fromisoformat(str(metrics["availability_timestamp"]))
        current = now or datetime.now(UTC)
        if current.tzinfo is None or current.utcoffset() is None:
            raise ForwardValidationError("Manual evidence comparison time must include an offset")
        if current < availability:
            raise ForwardValidationError("Manual observation is unavailable before local receipt")
        session_completed = datetime.fromisoformat(str(metrics["session_completed_at"]))
        receipt = datetime.fromisoformat(str(metrics["receipt_timestamp"]))
        market_date = date.fromisoformat(str(metrics["market_date"]))
        observations: dict[str, HistoricalBar] = {}
        for row in cast(list[dict[str, Any]], normalized.get("observations", [])):
            symbol = str(row["symbol"])
            if symbol in observations or symbol not in EXPANDED_UNIVERSE:
                raise ForwardValidationError("Normalized manual symbol mapping is invalid")
            if date.fromisoformat(str(row["market_date"])) != market_date:
                raise ForwardValidationError("Normalized manual market date mismatch")
            observations[symbol] = HistoricalBar(
                timestamp=session_completed,
                symbol=symbol,
                open=Decimal(str(row["open"])),
                high=Decimal(str(row["high"])),
                low=Decimal(str(row["low"])),
                close=Decimal(str(row["close"])),
                volume=int(row["volume"]),
                source=(
                    f"manual_dse:{metrics['raw_sha256']}:{metrics['normalized_sha256']}:"
                    f"{metrics['availability_timestamp']}"
                ),
                received_at=receipt,
                timestamp_provenance=TimestampProvenance.OPERATOR_ATTESTED,
                quality_flags=[
                    "forward_operator_attested",
                    "lineage_validated",
                    "raw_unadjusted",
                    "adjustment_view_unresolved",
                ],
            )
        expected = set(cast(list[str], metrics.get("eligible_symbols", [])))
        if set(observations) != expected:
            raise ForwardValidationError("Normalized manual symbol inventory mismatch")
        return {
            "market_date": market_date,
            "availability_timestamp": availability,
            "observations": observations,
            "missing_symbols": cast(list[str], metrics.get("missing_symbols", [])),
            "raw_sha256": metrics["raw_sha256"],
            "normalized_sha256": metrics["normalized_sha256"],
            "version": metrics["version"],
            "event_id": metrics["event_id"],
            "evidence_class": MANUAL_EVIDENCE_CLASS,
            "adjustment_grain": "raw_unadjusted",
            "analytical_adjustment_status": "unresolved",
        }

    def _consume_manual_ingests(
        self,
        session: PaperSession,
        *,
        now: datetime | None = None,
    ) -> list[dict[str, Any]]:
        consumed: list[dict[str, Any]] = []
        for ingest in self._manual_runs(session):
            loaded = self._load_manual_observation(ingest, now=now)
            event_id = _sha256(
                {
                    "manual_observation_consumed": session.id,
                    "ingest_event_id": loaded["event_id"],
                }
            )
            existing = self._run_by_event(session, event_id)
            if existing is not None:
                continue
            missing = cast(list[str], loaded["missing_symbols"])
            blockers = [
                "raw_unadjusted_forward_observation",
                "analytical_adjustment_view_unresolved",
                "period_end_calendar_evidence_unresolved",
            ]
            if missing:
                blockers.insert(0, "required_symbols_missing_without_substitution")
            metrics = {
                "mode": "forward",
                "evidence_class": MANUAL_EVIDENCE_CLASS,
                "manual_ingest_event_id": loaded["event_id"],
                "market_date": loaded["market_date"].isoformat(),
                "availability_timestamp": loaded["availability_timestamp"].isoformat(),
                "raw_sha256": loaded["raw_sha256"],
                "normalized_sha256": loaded["normalized_sha256"],
                "version": loaded["version"],
                "observed_symbols": sorted(cast(dict[str, HistoricalBar], loaded["observations"])),
                "missing_symbols": missing,
                "adjustment_grain": "raw_unadjusted",
                "analytical_adjustment_status": "unresolved",
                "decision_eligible": False,
                "decision_blockers": blockers,
                "orders_created": 0,
                "fills_created": 0,
                "transactions_created": 0,
            }
            observation = self._record(
                session,
                "observation",
                event_id,
                status="degraded" if missing else "completed",
                reason="; ".join(blockers),
                metrics=metrics,
            )
            self._record(
                session,
                "data_outage",
                _sha256({"manual_observation_blocked": event_id, "blockers": blockers}),
                status="degraded",
                reason="; ".join(blockers),
                metrics={**metrics, "trade_effect": False},
            )
            consumed.append(observation.metrics)
        return consumed

    def manual_ingestion_status(self, session: PaperSession | None = None) -> dict[str, Any]:
        current_session = session or self._session()
        if current_session is None:
            return {
                "boundary": "OPERATOR_ATTESTED_MANUAL",
                "accepted_versions": 0,
                "latest_market_date": None,
                "decision_eligible": False,
                "blockers": ["existing_authorized_forward_session_required"],
            }
        ingests = self._manual_runs(current_session)
        if not ingests:
            return {
                "boundary": "OPERATOR_ATTESTED_MANUAL",
                "accepted_versions": 0,
                "latest_market_date": None,
                "decision_eligible": False,
                "blockers": ["operator_attested_manual_eod_not_ingested"],
            }
        latest = ingests[-1].metrics
        blockers = list(cast(list[str], latest.get("decision_blockers", [])))
        if latest.get("missing_symbols"):
            blockers.insert(0, "required_symbols_missing_without_substitution")
        return {
            "boundary": "OPERATOR_ATTESTED_MANUAL",
            "automated_provider_certified": False,
            "accepted_versions": len(ingests),
            "latest_market_date": latest.get("market_date"),
            "latest_version": latest.get("version"),
            "latest_raw_sha256": latest.get("raw_sha256"),
            "latest_missing_symbols": latest.get("missing_symbols", []),
            "latest_availability_timestamp": latest.get("availability_timestamp"),
            "adjustment_grain": "raw_unadjusted",
            "decision_eligible": False,
            "blockers": list(dict.fromkeys(blockers)),
        }

    def _latest_control(self, session: PaperSession) -> PaperSessionRun | None:
        controls = {"runner_start", "operator_stop", "emergency_halt", "emergency_resume"}
        return next(
            (item for item in reversed(self._runs(session)) if item.run_type in controls), None
        )

    def _emergency_active(self, session: PaperSession) -> bool:
        latest = self._latest_control(session)
        return latest is not None and latest.run_type == "emergency_halt"

    def _activate(self, session: PaperSession, *, mode: str, resume_emergency: bool) -> None:
        risk = self.db.get(RiskState, 1)
        if risk is not None and risk.state in {"emergency_stop", "reconciliation_required"}:
            raise ForwardValidationError("Global emergency/reconciliation state blocks execution")
        if self._emergency_active(session) and not resume_emergency:
            raise ForwardValidationError("Emergency halt requires explicit --resume-emergency")
        reconciliation = self.reconcile(event_class="startup")
        if not reconciliation["healthy"]:
            raise ForwardValidationError("Startup reconciliation failed")
        if self._emergency_active(session):
            self._record(
                session,
                "emergency_resume",
                _sha256({"resume": datetime.now(UTC), "session": session.id}),
                metrics={"operator_action": "explicit_resume"},
            )
        if session.state == "configured":
            transition_session(self.db, session, "warming_up", "minimal_v1_forward_start")
            transition_session(self.db, session, "running", "startup_checks_passed")
        elif session.state in {"paused", "degraded", "reconciliation_required"}:
            transition_session(self.db, session, "running", "explicit_operator_start")
        elif session.state != "running":
            raise ForwardValidationError(f"Session cannot start from {session.state}")
        self._record(
            session,
            "runner_start",
            _sha256({"session": session.id, "mode": mode, "started": datetime.now(UTC)}),
            metrics={
                "mode": mode,
                "forward_validation_start_timestamp": (
                    datetime.now(UTC).isoformat() if mode == "forward" else None
                ),
                "replay_is_forward_evidence": False if mode == "replay" else None,
            },
        )

    def stop(self, reason: str = "explicit_operator_stop") -> dict[str, Any]:
        session = self._session()
        if session is None:
            return {"runtime_state": "STOPPED", "session": None}
        if session.state == "running":
            transition_session(self.db, session, "paused", reason)
        self._record(
            session,
            "operator_stop",
            _sha256({"session": session.id, "reason": reason, "at": datetime.now(UTC)}),
            metrics={"reason": reason},
        )
        return self.status()

    def emergency_halt(self, reason: str) -> dict[str, Any]:
        if len(reason.strip()) < 8:
            raise ValueError("Emergency reason must be explicit")
        session = self._session()
        if session is None:
            raise ForwardValidationError("Forward validation session does not exist")
        if session.state == "running":
            transition_session(self.db, session, "paused", f"emergency_halt: {reason}")
        self._record(
            session,
            "emergency_halt",
            _sha256({"session": session.id, "reason": reason, "at": datetime.now(UTC)}),
            status="halted",
            reason=reason,
            metrics={"holdings_preserved": True, "fabricated_liquidation": False},
        )
        self._alert("emergency_halt", reason, session=session)
        return self.status()

    def _alert(self, kind: str, message: str, *, session: PaperSession | None) -> None:
        path = self.repository_root / ALERT_RELATIVE_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size >= MAX_ALERT_BYTES:
            rotated = path.with_suffix(".log.1")
            if rotated.exists():
                rotated.unlink()
            path.replace(rotated)
        payload = {
            "timestamp": datetime.now(UTC).isoformat(),
            "kind": kind,
            "message": message,
            "session_id": session.id if session else None,
        }
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True) + "\n")
        print(f"FORWARD PAPER ALERT [{kind}] {message}", file=sys.stderr)

    def _account(self, session: PaperSession) -> PaperAccount:
        account = self.db.get(PaperAccount, session.account_id)
        if account is None:
            raise ForwardValidationError("Dedicated paper account is missing")
        return account

    def _holdings(self, session: PaperSession) -> dict[str, int]:
        holdings = {symbol: 0 for symbol in EXPANDED_UNIVERSE}
        transactions = self.db.scalars(
            select(Transaction)
            .where(Transaction.account_label == ACCOUNT_LABEL)
            .order_by(Transaction.occurred_at, Transaction.created_at)
        )
        for tx in transactions:
            quantity = int(tx.quantity)
            if tx.transaction_type in {"buy", "rights", "bonus"}:
                holdings[tx.symbol] = holdings.get(tx.symbol, 0) + quantity
            elif tx.transaction_type == "sell":
                holdings[tx.symbol] = holdings.get(tx.symbol, 0) - quantity
            if holdings.get(tx.symbol, 0) < 0:
                raise ForwardValidationError(f"Negative paper holding detected for {tx.symbol}")
        return holdings

    def reconcile(
        self, *, market_date: date | None = None, event_class: str = "operator"
    ) -> dict[str, Any]:
        session = self._session()
        if session is None:
            raise ForwardValidationError("Forward validation session does not exist")
        account = self._account(session)
        transaction_count = int(
            self.db.scalar(
                select(func.count())
                .select_from(Transaction)
                .where(Transaction.account_label == ACCOUNT_LABEL)
            )
            or 0
        )
        key = _sha256(
            {
                "reconciliation": session.id,
                "market_date": market_date,
                "event_class": event_class,
                "cash": account.cash,
                "transaction_count": transaction_count,
            }
        )
        existing = self._run_by_event(session, key)
        if existing is not None:
            existing_result = existing.metrics.get("result")
            if not isinstance(existing_result, dict):
                raise ForwardValidationError("Persisted reconciliation result is malformed")
            return existing_result
        broker = PaperBroker(self.db, account_id=session.account_id, account_label=ACCOUNT_LABEL)
        result = cast(dict[str, Any], broker.reconcile())
        holdings = self._holdings(session)
        ambiguous = self.db.scalar(
            select(Order).where(
                Order.idempotency_key.like(f"fv:{session.account_id}:%"),
                Order.status.in_(("submitted", "partially_filled")),
            )
        )
        result.update(
            {
                "holdings_nonnegative": all(value >= 0 for value in holdings.values()),
                "ambiguous_orders": ambiguous is not None,
            }
        )
        result["healthy"] = bool(
            result["healthy"] and result["holdings_nonnegative"] and not result["ambiguous_orders"]
        )
        self._record(
            session,
            "reconciliation",
            key,
            status="completed" if result["healthy"] else "failed",
            metrics={"market_date": market_date, "event_class": event_class, "result": result},
        )
        if not result["healthy"]:
            self._alert(
                "failed_reconciliation", json.dumps(result, sort_keys=True), session=session
            )
        return result

    def _validate_observation(
        self,
        day: date,
        observations: Mapping[str, HistoricalBar],
        *,
        replay: bool,
        now: datetime | None = None,
    ) -> None:
        missing = sorted(set(EXPANDED_UNIVERSE) - set(observations))
        extra = sorted(set(observations) - set(EXPANDED_UNIVERSE))
        if missing or extra:
            raise ForwardValidationError(
                f"Observation universe mismatch; missing={missing}, extra={extra}"
            )
        for symbol in EXPANDED_UNIVERSE:
            bar = observations[symbol]
            if bar.symbol != symbol or bar.timestamp.date() != day:
                raise ForwardValidationError(f"Timestamp or symbol mismatch for {symbol}")
            if not (
                Decimal("0")
                < bar.low
                <= min(bar.open, bar.close)
                <= max(bar.open, bar.close)
                <= bar.high
            ):
                raise ForwardValidationError(f"Malformed OHLC for {symbol}")
            if bar.volume is None or bar.volume < 0:
                raise ForwardValidationError(f"Missing or invalid volume for {symbol}")
            if replay:
                if "adjusted_execution" not in bar.quality_flags:
                    raise ForwardValidationError(
                        f"Replay adjustment/lineage boundary failed for {symbol}"
                    )
            else:
                if bar.timestamp_provenance not in TRUSTED_TIMESTAMPS:
                    raise ForwardValidationError(f"Timestamp trust insufficient for {symbol}")
                if not {"adjusted_execution", "lineage_validated"}.issubset(set(bar.quality_flags)):
                    raise ForwardValidationError(
                        f"Forward adjustment/lineage boundary failed for {symbol}"
                    )
                current = now or datetime.now(UTC)
                timestamp = bar.timestamp.replace(tzinfo=bar.timestamp.tzinfo or UTC)
                if (current - timestamp).total_seconds() > self.settings.DATA_MAX_STALENESS_SECONDS:
                    raise ForwardValidationError(f"Stale market data for {symbol}")

    def _snapshot_identity(
        self,
        day: date,
        visible: Mapping[str, Sequence[HistoricalBar]],
        datasets: Sequence[Mapping[str, Any]],
    ) -> str:
        return _sha256(
            {
                "as_of": day,
                "datasets": list(datasets),
                "visible_rows": {
                    symbol: [_bar_payload(bar) for bar in rows if bar.timestamp.date() <= day]
                    for symbol, rows in sorted(visible.items())
                },
            }
        )

    def _decision(
        self,
        session: PaperSession,
        day: date,
        execution_day: date,
        visible: Mapping[str, Sequence[HistoricalBar]],
        identity: Mapping[str, Any],
        *,
        replay: bool,
    ) -> dict[str, Any]:
        snapshot = self._snapshot_identity(
            day, visible, cast(list[dict[str, Any]], identity["datasets"])
        )
        event_id = _sha256(
            {
                "strategy_registration": identity["registration_id"],
                "rebalance_date": day,
                "snapshot": snapshot,
                "execution_session": execution_day,
            }
        )
        existing = self._run_by_event(session, event_id)
        if existing is not None:
            return existing.metrics
        scores, exclusions = absolute_momentum_scores(
            visible,
            day,
            lookback_months=PRIMARY_CONFIG.lookback_months,
            skip_recent_months=PRIMARY_CONFIG.skip_recent_months,
        )
        selected = sorted(symbol for symbol, score in scores.items() if score > 0)
        target = min(1 / len(selected), PRIMARY_CONFIG.maximum_symbol_weight) if selected else 0
        decision = {
            "mode": "replay" if replay else "forward",
            "replay_is_forward_evidence": False if replay else None,
            "decision_market_date": day.isoformat(),
            "decision_timestamp": datetime.combine(
                day, datetime_time(14, 30), ZoneInfo("Asia/Dhaka")
            )
            .astimezone(UTC)
            .isoformat(),
            "execution_session": execution_day.isoformat(),
            "data_snapshot_identity": snapshot,
            "scores": scores,
            "eligibility_exclusions": exclusions,
            "selected": selected,
            "ranking": selected,
            "target_weights": {symbol: target for symbol in selected},
            "unallocated_weight": 1 - target * len(selected),
            "same_bar_execution": False,
        }
        self._record(session, "decision", event_id, metrics=decision)
        self._alert(
            "rebalance_decision",
            f"{day.isoformat()} decision recorded for {execution_day.isoformat()}",
            session=session,
        )
        return {"event_id": event_id, **decision}

    def _order_key(
        self,
        session: PaperSession,
        decision: Mapping[str, Any],
        execution_day: date,
        symbol: str,
        side: str,
    ) -> str:
        frozen = cast(Mapping[str, Any], session.risk_profile)["frozen_identity"]
        digest = _sha256(
            {
                "registration": frozen["registration_id"],
                "rebalance_date": decision["decision_market_date"],
                "snapshot": decision["data_snapshot_identity"],
                "execution_session": execution_day,
                "symbol": symbol,
                "side": side,
            }
        )
        return f"fv:{session.account_id}:{digest}"

    def _execution(
        self,
        session: PaperSession,
        decision: Mapping[str, Any],
        execution_day: date,
        observations: Mapping[str, HistoricalBar],
        *,
        replay: bool,
    ) -> dict[str, Any]:
        if self._emergency_active(session):
            raise ForwardValidationError("Emergency halt prevents paper execution")
        risk = self.db.get(RiskState, 1)
        if risk is not None and risk.state in {"emergency_stop", "reconciliation_required"}:
            raise ForwardValidationError("Global emergency/reconciliation state prevents execution")
        execution_id = _sha256(
            {"decision": decision["event_id"], "execution_session": execution_day}
        )
        existing = self._run_by_event(session, execution_id)
        if existing is not None:
            return existing.metrics
        account = self._account(session)
        holdings = self._holdings(session)
        execution_snapshot = _sha256(
            {symbol: _bar_payload(bar) for symbol, bar in sorted(observations.items())}
        )
        plan_id = _sha256({"execution_plan": execution_id})
        persisted_plan = self._run_by_event(session, plan_id)
        if persisted_plan is None:
            opening_equity = account.cash + sum(
                Decimal(holdings[symbol]) * observations[symbol].open
                for symbol in EXPANDED_UNIVERSE
            )
            weights = cast(Mapping[str, float], decision["target_weights"])
            targets: dict[str, int] = {}
            for symbol in EXPANDED_UNIVERSE:
                weight = Decimal(str(weights.get(symbol, 0)))
                buy = observations[symbol].open * (Decimal("1") + SLIPPAGE_RATE)
                targets[symbol] = int(
                    (opening_equity * weight / (buy * (Decimal("1") + FEE_RATE))).to_integral_value(
                        rounding=ROUND_DOWN
                    )
                )
            persisted_plan = self._record(
                session,
                "execution_plan",
                plan_id,
                metrics={
                    "decision_event_id": decision["event_id"],
                    "execution_session": execution_day,
                    "execution_data_snapshot_identity": execution_snapshot,
                    "opening_equity": opening_equity,
                    "opening_cash": account.cash,
                    "opening_holdings": holdings,
                    "target_quantities": targets,
                },
            )
        plan = persisted_plan.metrics
        if plan.get("execution_data_snapshot_identity") != execution_snapshot:
            raise ForwardValidationError("Execution data changed after durable planning")
        targets = {symbol: int(value) for symbol, value in plan["target_quantities"].items()}
        fills: list[dict[str, Any]] = []
        rejections: list[dict[str, Any]] = []
        broker = PaperBroker(
            self.db,
            participation_rate=Decimal("1"),
            slippage_percent=Decimal("0.25"),
            fill_model="pessimistic",
            account_id=session.account_id,
            account_label=ACCOUNT_LABEL,
            source_metadata={
                "forward_session_id": session.id,
                "decision_event_id": str(decision["event_id"]),
                "execution_session": execution_day.isoformat(),
                "execution_data_snapshot_identity": execution_snapshot,
                "evidence_class": "simulation_replay" if replay else "forward_paper",
            },
        )

        def execute(symbol: str, side: str, desired: int) -> None:
            if desired <= 0:
                return
            volume = int(observations[symbol].volume or 0)
            quantity = min(desired, volume)
            if quantity < desired:
                rejections.append(
                    {
                        "symbol": symbol,
                        "side": side,
                        "quantity": desired - quantity,
                        "reason": "source_volume_limit",
                    }
                )
            source_open = observations[symbol].open
            limit = (
                source_open * (Decimal("1") + SLIPPAGE_RATE)
                if side == "buy"
                else source_open * (Decimal("1") - SLIPPAGE_RATE)
            ).quantize(Decimal("0.01"))
            if side == "buy":
                affordable = int(self._account(session).cash / (limit * (Decimal("1") + FEE_RATE)))
                if affordable < quantity:
                    rejections.append(
                        {
                            "symbol": symbol,
                            "side": side,
                            "quantity": quantity - max(affordable, 0),
                            "reason": "insufficient_paper_cash",
                        }
                    )
                    quantity = max(affordable, 0)
            if quantity <= 0:
                return
            key = self._order_key(session, decision, execution_day, symbol, side)
            order = self.db.scalar(select(Order).where(Order.idempotency_key == key))
            if order is None:
                now = datetime.now(UTC)
                order = Order(
                    idempotency_key=key,
                    symbol=symbol,
                    side=side,
                    order_type="limit",
                    quantity=quantity,
                    limit_price=limit,
                    status="approved",
                    strategy_id=STRATEGY_IDENTITY,
                    created_at=now,
                    updated_at=now,
                )
                self.db.add(order)
                self.db.commit()
            elif order.status in {"submitted", "partially_filled"}:
                raise ForwardValidationError(f"Ambiguous prior paper effect for {symbol}")
            elif order.status == "filled":
                fills.append(
                    {
                        "order_id": order.id,
                        "symbol": symbol,
                        "side": side,
                        "quantity": order.filled_quantity,
                        "fill_price": str(order.average_fill_price),
                        "recovered_completed_effect": True,
                    }
                )
                return
            elif order.status != "approved":
                rejections.append(
                    {"symbol": symbol, "side": side, "quantity": quantity, "reason": order.status}
                )
                return
            order = broker.submit_order(order, source_open, volume)
            if order.status != "filled":
                raise ForwardValidationError(
                    f"Paper order did not complete deterministically: {symbol} {order.status}"
                )
            fill_price = cast(Decimal, order.average_fill_price)
            fee = (Decimal(order.filled_quantity) * fill_price * FEE_RATE).quantize(Decimal("0.01"))
            slippage = (abs(fill_price - source_open) * order.filled_quantity).quantize(
                Decimal("0.01")
            )
            fills.append(
                {
                    "order_id": order.id,
                    "idempotency_key": key,
                    "symbol": symbol,
                    "side": side,
                    "target_quantity": targets[symbol],
                    "quantity": order.filled_quantity,
                    "source_open": str(source_open),
                    "fill_price": str(fill_price),
                    "fee": str(fee),
                    "slippage": str(slippage),
                }
            )

        current = self._holdings(session)
        for symbol in sorted(EXPANDED_UNIVERSE):
            execute(symbol, "sell", current[symbol] - targets[symbol])
        current = self._holdings(session)
        for symbol in cast(list[str], decision["selected"]):
            execute(symbol, "buy", targets[symbol] - current[symbol])
        result = {
            "mode": "replay" if replay else "forward",
            "replay_is_forward_evidence": False if replay else None,
            "decision_event_id": decision["event_id"],
            "execution_session": execution_day.isoformat(),
            "execution_data_snapshot_identity": execution_snapshot,
            "target_quantities": targets,
            "fills": fills,
            "rejected_quantities": rejections,
            "cash": str(self._account(session).cash),
            "holdings": self._holdings(session),
            "cost_contract": {"fee_percent": "0.40", "slippage_percent": "0.25"},
        }
        self._record(session, "execution", execution_id, metrics=result)
        self._alert(
            "paper_execution",
            f"{execution_day.isoformat()} completed with {len(fills)} fills",
            session=session,
        )
        return {"event_id": execution_id, **result}

    def _benchmark(
        self,
        session: PaperSession,
        observations: Mapping[str, HistoricalBar],
        prior: Sequence[PaperSessionRun],
    ) -> tuple[dict[str, int], Decimal, Decimal]:
        if prior:
            first = prior[0].metrics
            quantities = {
                symbol: int(value) for symbol, value in first["benchmark_quantities"].items()
            }
            cash = Decimal(str(first["benchmark_cash"]))
        else:
            allocation = session.starting_cash / len(EXPANDED_UNIVERSE)
            quantities = {
                symbol: int(
                    (allocation / observations[symbol].close).to_integral_value(rounding=ROUND_DOWN)
                )
                for symbol in EXPANDED_UNIVERSE
            }
            invested = sum(
                Decimal(quantities[symbol]) * observations[symbol].close
                for symbol in EXPANDED_UNIVERSE
            )
            cash = session.starting_cash - invested
        value = cash + sum(
            Decimal(quantities[symbol]) * observations[symbol].close for symbol in EXPANDED_UNIVERSE
        )
        return quantities, cash, value

    def _daily_summary(
        self,
        session: PaperSession,
        day: date,
        observations: Mapping[str, HistoricalBar],
        reconciliation: Mapping[str, Any],
        *,
        replay: bool,
        started_wall: datetime,
    ) -> dict[str, Any]:
        mode = "replay" if replay else "forward"
        event_id = _sha256({"daily_summary": session.id, "day": day, "mode": mode})
        existing = self._run_by_event(session, event_id)
        if existing is not None:
            return existing.metrics
        account = self._account(session)
        holdings = self._holdings(session)
        invested = sum(
            Decimal(holdings[symbol]) * observations[symbol].close for symbol in EXPANDED_UNIVERSE
        )
        equity = account.cash + invested
        prior = [
            item
            for item in self._runs(session, "daily_summary")
            if item.metrics.get("mode") == mode
        ]
        prior_equities = [Decimal(str(item.metrics["equity"])) for item in prior]
        peak = max([session.starting_cash, *prior_equities, equity])
        drawdown = (equity / peak - 1) * 100 if peak else Decimal("0")
        transactions = list(
            self.db.scalars(select(Transaction).where(Transaction.account_label == ACCOUNT_LABEL))
        )
        traded = sum(
            tx.quantity * tx.price for tx in transactions if tx.transaction_type in {"buy", "sell"}
        )
        executions = [
            item for item in self._runs(session, "execution") if item.metrics.get("mode") == mode
        ]
        fees = sum(
            (Decimal(str(fill["fee"])) for item in executions for fill in item.metrics["fills"]),
            Decimal("0"),
        )
        slippage = sum(
            (
                Decimal(str(fill["slippage"]))
                for item in executions
                for fill in item.metrics["fills"]
                if "slippage" in fill
            ),
            Decimal("0"),
        )
        average_equity = mean(float(value) for value in [*prior_equities, equity])
        benchmark_quantities, benchmark_cash, benchmark_value = self._benchmark(
            session, observations, prior
        )
        summary = {
            "mode": mode,
            "replay_is_forward_evidence": False if replay else None,
            "market_date": day.isoformat(),
            "data_snapshot_identity": _sha256(
                {symbol: _bar_payload(bar) for symbol, bar in sorted(observations.items())}
            ),
            "cash": str(account.cash),
            "holdings": holdings,
            "equity": str(equity),
            "cumulative_paper_return_percent": float((equity / session.starting_cash - 1) * 100),
            "benchmark_quantities": benchmark_quantities,
            "benchmark_cash": str(benchmark_cash),
            "benchmark_return_percent": float((benchmark_value / session.starting_cash - 1) * 100),
            "drawdown_percent": float(drawdown),
            "turnover": float(traded) / average_equity if average_equity else 0.0,
            "fees_bdt": str(fees),
            "slippage_bdt": str(slippage),
            "invested_exposure_percent": float(invested / equity * 100) if equity else 0.0,
            "cash_exposure_percent": float(account.cash / equity * 100) if equity else 0.0,
            "decisions": sum(
                item.metrics.get("mode") == mode for item in self._runs(session, "decision")
            ),
            "paper_executions": len(executions),
            "reconciliation_failures": sum(
                item.status == "failed" for item in self._runs(session, "reconciliation")
            ),
            "data_outages": len(self._runs(session, "data_outage")),
            "uptime_seconds": max(0.0, (datetime.now(UTC) - started_wall).total_seconds()),
            "reconciliation": dict(reconciliation),
            "runner_health": "HEALTHY" if reconciliation.get("healthy") else "HALTED",
        }
        self._record(session, "daily_summary", event_id, metrics=summary)
        return {"event_id": event_id, **summary}

    def run_replay(
        self,
        start: date,
        end: date,
        *,
        starting_cash: Decimal = DEFAULT_STARTING_CASH,
        resume_emergency: bool = False,
    ) -> dict[str, Any]:
        if self.settings.DATABASE_ROLE != "test":
            raise ForwardValidationError("Replay requires an isolated test-role database")
        if start > end:
            raise ValueError("Replay start must not be after end")
        startup, loaded = self.verify_startup()
        identity = cast(dict[str, Any], startup["strategy"])
        session = self.ensure_session(identity, starting_cash=starting_cash)
        self._activate(session, mode="replay", resume_emergency=resume_emergency)
        started_wall = datetime.now(UTC)
        by_symbol = {
            symbol: {bar.timestamp.date(): bar for bar in rows}
            for symbol, rows in loaded.bars.items()
        }
        common_dates = sorted(set.intersection(*(set(rows) for rows in by_symbol.values())))
        replay_dates = [day for day in common_dates if start <= day <= end]
        if not replay_dates:
            raise ForwardValidationError("Replay window has no common source-present sessions")
        endings = quarter_end_sessions(common_dates)
        next_session = {
            common_dates[index]: common_dates[index + 1] for index in range(len(common_dates) - 1)
        }
        summaries: list[dict[str, Any]] = []
        for day in replay_dates:
            observations = {symbol: by_symbol[symbol][day] for symbol in EXPANDED_UNIVERSE}
            try:
                self._validate_observation(day, observations, replay=True)
                self._record(
                    session,
                    "observation",
                    _sha256({"observation": session.id, "day": day, "mode": "replay"}),
                    metrics={
                        "mode": "replay",
                        "replay_is_forward_evidence": False,
                        "market_date": day,
                        "data_snapshot_identity": _sha256(
                            {
                                symbol: _bar_payload(bar)
                                for symbol, bar in sorted(observations.items())
                            }
                        ),
                    },
                )
                for decision_run in self._runs(session, "decision"):
                    decision = decision_run.metrics
                    if decision.get("execution_session") == day.isoformat():
                        self._execution(session, decision, day, observations, replay=True)
                if day in endings and day in next_session:
                    visible = {
                        symbol: [bar for bar in loaded.bars[symbol] if bar.timestamp.date() <= day]
                        for symbol in EXPANDED_UNIVERSE
                    }
                    self._decision(
                        session,
                        day,
                        next_session[day],
                        visible,
                        identity,
                        replay=True,
                    )
                reconciliation = self.reconcile(market_date=day, event_class="replay")
                if not reconciliation["healthy"]:
                    raise ForwardValidationError("Replay reconciliation failed")
                summaries.append(
                    self._daily_summary(
                        session,
                        day,
                        observations,
                        reconciliation,
                        replay=True,
                        started_wall=started_wall,
                    )
                )
                session.heartbeat_at = datetime.now(UTC)
                self.db.commit()
            except Exception as exc:
                self._record(
                    session,
                    "data_outage",
                    _sha256({"data_failure": session.id, "day": day, "error": str(exc)}),
                    status="failed",
                    reason=str(exc),
                    metrics={"mode": "replay", "market_date": day, "trade_effect": False},
                )
                self._alert("replay_halted", str(exc), session=session)
                raise
        self.stop("replay_completed_without_forward_evidence")
        return {
            "mode": "replay",
            "replay_is_forward_evidence": False,
            "session_id": session.id,
            "account_id": session.account_id,
            "account_label": ACCOUNT_LABEL,
            "market_start": replay_dates[0].isoformat(),
            "market_end": replay_dates[-1].isoformat(),
            "sessions_processed": len(replay_dates),
            "decisions": len(self._runs(session, "decision")),
            "executions": len(self._runs(session, "execution")),
            "latest_summary": summaries[-1],
            "ledger_path": str(self._ledger_path(session)),
        }

    def run_forever(
        self,
        *,
        starting_cash: Decimal = DEFAULT_STARTING_CASH,
        poll_seconds: int = 60,
        resume_emergency: bool = False,
    ) -> None:
        startup, _ = self.verify_startup()
        session = self.ensure_session(
            cast(dict[str, Any], startup["strategy"]), starting_cash=starting_cash
        )
        self._activate(session, mode="forward", resume_emergency=resume_emergency)
        while True:
            self.db.refresh(session)
            if session.state != "running" or self._emergency_active(session):
                return
            risk = self.db.get(RiskState, 1)
            if risk is not None and risk.state in {"emergency_stop", "reconciliation_required"}:
                self.emergency_halt(f"global risk state: {risk.state}")
                return
            readiness = self.provider_readiness()
            manual = self.manual_ingestion_status(session)
            try:
                self._consume_manual_ingests(session)
            except ForwardValidationError as exc:
                integrity_event = _sha256(
                    {
                        "manual_evidence_integrity_failure": session.id,
                        "day": date.today(),
                        "error": str(exc),
                    }
                )
                self._record(
                    session,
                    "data_outage",
                    integrity_event,
                    status="failed",
                    reason=str(exc),
                    metrics={
                        "mode": "forward",
                        "boundary": "OPERATOR_ATTESTED_MANUAL",
                        "trade_effect": False,
                    },
                )
                self._alert("manual_forward_evidence_blocked", str(exc), session=session)
                return
            if not readiness["ready"]:
                blockers = (
                    cast(list[str], manual["blockers"])
                    if manual["accepted_versions"]
                    else cast(list[str], readiness["blockers"])
                )
                event_id = _sha256(
                    {
                        "provider_blocked": session.id,
                        "day": date.today(),
                        "blockers": blockers,
                    }
                )
                already_recorded = self._run_by_event(session, event_id) is not None
                self._record(
                    session,
                    "data_outage",
                    event_id,
                    status="degraded",
                    reason="; ".join(blockers),
                    metrics={
                        "mode": "forward",
                        "readiness": readiness,
                        "manual_ingestion": manual,
                        "trade_effect": False,
                    },
                )
                if not already_recorded:
                    self._alert(
                        "forward_data_blocked",
                        "; ".join(blockers),
                        session=session,
                    )
            session.heartbeat_at = datetime.now(UTC)
            self.db.commit()
            time.sleep(max(5, poll_seconds))

    def latest_decision(self) -> dict[str, Any] | None:
        session = self._session()
        if session is None:
            return None
        rows = self._runs(session, "decision")
        return rows[-1].metrics if rows else None

    def portfolio(self) -> dict[str, Any]:
        session = self._session()
        if session is None:
            return {"session": None, "cash": None, "holdings": {}}
        daily = self._runs(session, "daily_summary")
        latest = daily[-1].metrics if daily else {}
        return {
            "session_id": session.id,
            "account_id": session.account_id,
            "account_label": ACCOUNT_LABEL,
            "cash": str(self._account(session).cash),
            "holdings": self._holdings(session),
            "latest_market_date": latest.get("market_date"),
            "latest_equity": latest.get("equity"),
            "evidence_class": latest.get("mode"),
        }

    def status(self) -> dict[str, Any]:
        session = self._session()
        provider = self.provider_readiness()
        manual = self.manual_ingestion_status(session)
        if session is None:
            return {
                "runtime_state": "STOPPED",
                "strategy": STRATEGY_IDENTITY,
                "session": None,
                "provider_readiness": provider,
                "manual_ingestion": manual,
                "qualification": "0/60",
            }
        runs = self._runs(session)
        latest = runs[-1] if runs else None
        risk = self.db.get(RiskState, 1)
        if (
            self._emergency_active(session)
            or (risk is not None and risk.state in {"emergency_stop", "reconciliation_required"})
            or not verify_audit_chain(self.db)
        ):
            runtime = "HALTED"
        elif latest is not None and latest.status in {"failed", "degraded"}:
            runtime = "DEGRADED"
        elif session.state != "running":
            runtime = "STOPPED"
        elif not provider["ready"]:
            runtime = "DEGRADED"
        else:
            runtime = "HEALTHY"
        replay = [
            item
            for item in self._runs(session, "daily_summary")
            if item.metrics.get("mode") == "replay"
        ]
        forward = [
            item
            for item in self._runs(session, "daily_summary")
            if item.metrics.get("mode") == "forward"
        ]
        return {
            "runtime_state": runtime,
            "strategy": STRATEGY_IDENTITY,
            "session_id": session.id,
            "account_id": session.account_id,
            "session_state": session.state,
            "heartbeat_at": session.heartbeat_at.isoformat() if session.heartbeat_at else None,
            "emergency_halt": self._emergency_active(session),
            "provider_readiness": provider,
            "manual_ingestion": manual,
            "latest_decision": self.latest_decision(),
            "latest_replay_metrics": replay[-1].metrics if replay else None,
            "latest_forward_metrics": forward[-1].metrics if forward else None,
            "ledger_path": str(self._ledger_path(session)),
            "qualification": "0/60",
        }


__all__ = [
    "ACCOUNT_LABEL",
    "DEFAULT_STARTING_CASH",
    "ForwardPaperValidationRunner",
    "ForwardValidationError",
    "MANUAL_ATTESTATION",
    "MANUAL_EVIDENCE_CLASS",
    "MANUAL_SOURCE_IDENTITY",
    "RunnerLock",
    "SESSION_NAME",
    "quarter_end_sessions",
]
