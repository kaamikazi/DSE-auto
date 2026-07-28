from __future__ import annotations

import csv
import hashlib
import json
import re
import sqlite3
from collections import Counter
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from html import escape
from itertools import groupby
from pathlib import Path
from typing import Any, TextIO

TRANSFORMATION_VERSION = "canonical-dse-candidate-v1"
INVALID_CATEGORIES = (
    "missing_open",
    "missing_high",
    "missing_low",
    "missing_close",
    "negative_price",
    "high_below_low",
    "open_outside_range",
    "close_outside_range",
    "negative_volume",
    "non_numeric_value",
    "zero_price",
    "duplicate_exact",
    "duplicate_conflicting",
    "impossible_date",
    "symbol_missing",
    "corporate_action_candidate",
    "suspected_source_corruption",
    "unresolved",
)
REJECTION_CATEGORIES = {
    "missing_open",
    "missing_high",
    "missing_low",
    "missing_close",
    "negative_price",
    "high_below_low",
    "open_outside_range",
    "close_outside_range",
    "negative_volume",
    "non_numeric_value",
    "zero_price",
    "impossible_date",
    "symbol_missing",
    "suspected_source_corruption",
}
REQUIRED_FIELDS = ("symbol", "date", "open", "high", "low", "close", "volume")
FIELD_ALIASES = {
    "symbol": ("symbol", "trading_code", "ticker", "scrip", "code"),
    "date": ("date", "trading_date"),
    "open": ("open",),
    "high": ("high",),
    "low": ("low",),
    "close": ("close",),
    "volume": ("volume",),
}
QUALITY_WEIGHTS = {
    "schema_completeness": 12,
    "duplicate_rate": 12,
    "invalid_row_rate": 12,
    "conflict_rate": 14,
    "date_coverage": 8,
    "symbol_coverage": 8,
    "adjustment_documentation": 8,
    "licensing_clarity": 8,
    "timestamp_provenance": 8,
    "reproducibility": 6,
    "cross_source_agreement": 4,
}
INDEX_ALIASES = {"00DSEX": "DSEX", "00DS30": "DS30", "00DSES": "DSES"}
CORPORATE_ACTION_CLASSIFICATIONS = (
    "probable_split",
    "probable_bonus_share_adjustment",
    "probable_rights_issue",
    "probable_dividend_adjustment",
    "possible_suspension_resumption",
    "ordinary_market_movement",
    "unresolved",
)


@dataclass(frozen=True)
class DatasetSource:
    dataset_id: str
    source_hash: str
    source_name: str
    source_path: str
    adjustment_status: str
    source_trust: str
    timestamp_trust: str
    license_note: str
    stated_row_count: int | None = None
    stated_symbol_count: int | None = None
    stated_symbol_claim: str = ""
    logical_name: str = ""


@dataclass(frozen=True)
class SymbolMapping:
    original_symbol: str
    normalized_symbol: str
    mapping_reason: str
    instrument_class: str
    confidence: str
    evidence_source: str
    approval_status: str
    effective_from: str | None = None
    effective_to: str | None = None


@dataclass(frozen=True)
class ParsedObservation:
    source_dataset_id: str
    source_hash: str
    source_name: str
    source_row_id: str
    original_symbol: str
    normalized_symbol: str
    raw_date: str
    trading_date: str | None
    raw_open: str
    raw_high: str
    raw_low: str
    raw_close: str
    raw_volume: str
    open: str | None
    high: str | None
    low: str | None
    close: str | None
    volume: str | None
    adjustment_status: str
    invalid_categories: tuple[str, ...]
    warnings: tuple[str, ...]
    mapping_reason: str
    mapping_confidence: str
    mapping_approval_status: str
    instrument_class: str
    transformation_version: str = TRANSFORMATION_VERSION
    transformation_reason: str = "deterministic schema normalization and validation"

    @property
    def accepted_for_candidate(self) -> bool:
        return not REJECTION_CATEGORIES.intersection(self.invalid_categories)

    @property
    def value_fingerprint(self) -> str:
        payload = [self.open, self.high, self.low, self.close, self.volume]
        return hashlib.sha256(json.dumps(payload, separators=(",", ":")).encode()).hexdigest()


@dataclass
class InventoryAccumulator:
    source: DatasetSource
    observed_rows: int = 0
    encoding_issues: int = 0
    schema_inconsistencies: int = 0
    symbols: set[str] = field(default_factory=set)
    dates: set[str] = field(default_factory=set)
    categories: Counter[str] = field(default_factory=Counter)
    warnings: Counter[str] = field(default_factory=Counter)


def _normalized_header(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")


def infer_mapping(fieldnames: Sequence[str]) -> dict[str, str]:
    available = {_normalized_header(item): item for item in fieldnames}
    result: dict[str, str] = {}
    for target, aliases in FIELD_ALIASES.items():
        for alias in aliases:
            if alias in available:
                result[target] = available[alias]
                break
    return result


def parse_trading_date(value: str) -> date | None:
    cleaned = value.strip()
    for pattern in ("%Y-%m-%d", "%d-%m-%Y", "%Y%m%d", "%d/%m/%Y"):
        try:
            parsed = datetime.strptime(cleaned, pattern).date()
            if date(1990, 1, 1) <= parsed <= date.today() + timedelta(days=2):
                return parsed
            return None
        except ValueError:
            continue
    return None


def normalize_symbol(original: str, evidence_source: str) -> SymbolMapping:
    stripped = original.strip()
    upper = stripped.upper()
    if not upper:
        return SymbolMapping(
            original,
            "",
            "missing_symbol",
            "unknown",
            "high",
            evidence_source,
            "rejected",
        )
    if upper in INDEX_ALIASES:
        return SymbolMapping(
            original,
            INDEX_ALIASES[upper],
            "known_index_alias_candidate",
            "index",
            "high",
            evidence_source,
            "under_review",
        )
    if upper in {"DSEX", "DS30", "DSES"}:
        return SymbolMapping(
            original,
            upper,
            "canonical_index_label",
            "index",
            "high",
            evidence_source,
            "not_required_format_only",
        )
    if re.search(r"\s", upper):
        compact = re.sub(r"\s+", "", upper)
        return SymbolMapping(
            original,
            compact,
            "whitespace_removed_candidate",
            "unknown",
            "medium",
            evidence_source,
            "under_review",
        )
    if not re.fullmatch(r"[A-Z0-9&()._-]+", upper):
        return SymbolMapping(
            original,
            upper,
            "malformed_symbol",
            "unknown",
            "low",
            evidence_source,
            "under_review",
        )
    instrument_class = "equity"
    if upper.endswith("MF") or "MUTUAL" in upper:
        instrument_class = "fund"
    elif "BOND" in upper or upper.endswith("IBBLPBOND"):
        instrument_class = "bond"
    return SymbolMapping(
        original,
        upper,
        "case_or_identity_normalization",
        instrument_class,
        "high",
        evidence_source,
        "not_required_format_only",
    )


def _decimal(raw: str) -> tuple[Decimal | None, bool]:
    cleaned = raw.strip().replace(",", "")
    if not cleaned:
        return None, False
    try:
        value = Decimal(cleaned)
        return value if value.is_finite() else None, value.is_finite()
    except InvalidOperation:
        return None, False


def classify_ohlcv(
    *,
    raw_date: str,
    symbol: str,
    raw_open: str,
    raw_high: str,
    raw_low: str,
    raw_close: str,
    raw_volume: str,
) -> tuple[tuple[str, ...], tuple[str, ...], dict[str, str | None]]:
    categories: set[str] = set()
    warnings: set[str] = set()
    if not symbol.strip():
        categories.add("symbol_missing")
    parsed_date = parse_trading_date(raw_date)
    if parsed_date is None:
        categories.add("impossible_date")
    elif parsed_date.weekday() in {4, 5}:
        warnings.add("non_trading_day_observed")
    raw_values = {
        "open": raw_open,
        "high": raw_high,
        "low": raw_low,
        "close": raw_close,
        "volume": raw_volume,
    }
    values: dict[str, Decimal | None] = {}
    for field_name, raw in raw_values.items():
        if not raw.strip() and field_name != "volume":
            categories.add(f"missing_{field_name}")
        value, numeric = _decimal(raw)
        values[field_name] = value
        if raw.strip() and not numeric:
            categories.add("non_numeric_value")
    prices = [values[name] for name in ("open", "high", "low", "close")]
    if any(value is not None and value < 0 for value in prices):
        categories.add("negative_price")
    if any(value == 0 for value in prices if value is not None):
        categories.add("zero_price")
    volume = values["volume"]
    if volume is not None and volume < 0:
        categories.add("negative_volume")
    if volume == 0:
        warnings.add("zero_volume")
    open_, high, low, close = (values[name] for name in ("open", "high", "low", "close"))
    if high is not None and low is not None and high < low:
        categories.add("high_below_low")
    if open_ is not None and high is not None and low is not None and not low <= open_ <= high:
        categories.add("open_outside_range")
    if close is not None and high is not None and low is not None and not low <= close <= high:
        categories.add("close_outside_range")
    structural = categories.intersection(
        {"high_below_low", "open_outside_range", "close_outside_range", "non_numeric_value"}
    )
    if len(structural) >= 2:
        categories.add("suspected_source_corruption")
    normalized = {name: str(value) if value is not None else None for name, value in values.items()}
    normalized["date"] = parsed_date.isoformat() if parsed_date else None
    return tuple(sorted(categories)), tuple(sorted(warnings)), normalized


def parse_observation(
    source: DatasetSource,
    source_row_id: str,
    row: dict[str, str],
    mapping: dict[str, str],
) -> tuple[ParsedObservation, SymbolMapping]:
    values = {name: str(row.get(mapping.get(name, ""), "") or "") for name in REQUIRED_FIELDS}
    symbol_mapping = normalize_symbol(values["symbol"], source.source_name)
    categories, warnings, normalized = classify_ohlcv(
        raw_date=values["date"],
        symbol=values["symbol"],
        raw_open=values["open"],
        raw_high=values["high"],
        raw_low=values["low"],
        raw_close=values["close"],
        raw_volume=values["volume"],
    )
    return (
        ParsedObservation(
            source_dataset_id=source.dataset_id,
            source_hash=source.source_hash,
            source_name=source.source_name,
            source_row_id=source_row_id,
            original_symbol=values["symbol"],
            normalized_symbol=symbol_mapping.normalized_symbol,
            raw_date=values["date"],
            trading_date=normalized["date"],
            raw_open=values["open"],
            raw_high=values["high"],
            raw_low=values["low"],
            raw_close=values["close"],
            raw_volume=values["volume"],
            open=normalized["open"],
            high=normalized["high"],
            low=normalized["low"],
            close=normalized["close"],
            volume=normalized["volume"],
            adjustment_status=source.adjustment_status,
            invalid_categories=categories,
            warnings=warnings,
            mapping_reason=symbol_mapping.mapping_reason,
            mapping_confidence=symbol_mapping.confidence,
            mapping_approval_status=symbol_mapping.approval_status,
            instrument_class=symbol_mapping.instrument_class,
        ),
        symbol_mapping,
    )


def duplicate_classification(rows: Sequence[ParsedObservation]) -> tuple[str, str]:
    fingerprints = {row.value_fingerprint for row in rows}
    originals = {row.original_symbol for row in rows}
    adjustments = {row.adjustment_status for row in rows}
    price_fingerprints = {(row.open, row.high, row.low, row.close) for row in rows}
    volumes = {row.volume for row in rows}
    if len(adjustments) > 1:
        return "adjusted_unadjusted_duplicate", "adjusted_unadjusted_split"
    if len(originals) > 1:
        return "multiple_instruments_malformed_symbol", "manual_review_required"
    if len(fingerprints) == 1:
        return "duplicate_exact", "safe_exact_deduplication"
    if len(price_fingerprints) == 1 and len(volumes) > 1:
        return "same_price_different_volume", "manual_review_required"
    if len(price_fingerprints) > 1:
        return "conflicting_ohlc", "manual_review_required"
    return "unresolved_conflict", "unresolved"


def compare_values(
    left: ParsedObservation, right: ParsedObservation, tolerance: Decimal
) -> dict[str, Any]:
    fields = ("open", "high", "low", "close", "volume")
    absolute: dict[str, str | None] = {}
    percentage: dict[str, str | None] = {}
    within = True
    for field_name in fields:
        left_raw, right_raw = getattr(left, field_name), getattr(right, field_name)
        if left_raw is None or right_raw is None:
            absolute[field_name] = None
            percentage[field_name] = None
            within = False
            continue
        a, b = Decimal(left_raw), Decimal(right_raw)
        difference = abs(a - b)
        relative = difference / max(abs(a), abs(b), Decimal("0.0001"))
        absolute[field_name] = str(difference)
        percentage[field_name] = str(relative)
        within = within and relative <= tolerance
    return {
        "absolute_differences": absolute,
        "percentage_differences": percentage,
        "volume_difference": absolute["volume"],
        "tolerance_result": "within_tolerance" if within else "outside_tolerance",
        "exact_match": left.value_fingerprint == right.value_fingerprint,
    }


def corporate_action_classification(
    previous_close: Decimal,
    current_close: Decimal,
    *,
    adjusted_close: Decimal | None = None,
    unadjusted_close: Decimal | None = None,
    gap_days: int = 1,
) -> str:
    if previous_close <= 0 or current_close <= 0:
        return "unresolved"
    if (
        adjusted_close is not None
        and unadjusted_close is not None
        and adjusted_close != unadjusted_close
    ):
        ratio = max(adjusted_close, unadjusted_close) / min(adjusted_close, unadjusted_close)
        if any(
            abs(ratio - Decimal(candidate)) <= Decimal("0.03") for candidate in (2, 3, 4, 5, 10)
        ):
            return "probable_split"
        if Decimal("1.05") <= ratio < Decimal("2"):
            return "probable_bonus_share_adjustment"
        return "probable_dividend_adjustment"
    change = abs(current_close / previous_close - 1)
    if change < Decimal("0.20"):
        return "ordinary_market_movement"
    if gap_days > 10:
        return "possible_suspension_resumption"
    ratio = max(previous_close, current_close) / min(previous_close, current_close)
    if any(abs(ratio - Decimal(candidate)) <= Decimal("0.03") for candidate in (2, 3, 4, 5, 10)):
        return "probable_split"
    if Decimal("1.2") <= ratio < Decimal("2"):
        return "probable_bonus_share_adjustment"
    return "unresolved"


def calendar_analysis(dates: Iterable[str]) -> dict[str, Any]:
    parsed = sorted({date.fromisoformat(item) for item in dates})
    weekdays = Counter(item.strftime("%A") for item in parsed)
    weekend = [item.isoformat() for item in parsed if item.weekday() in {4, 5}]
    long_gaps: list[dict[str, Any]] = []
    for left, right in zip(parsed, parsed[1:], strict=False):
        days = (right - left).days
        if days > 7:
            long_gaps.append({"after": left.isoformat(), "before": right.isoformat(), "days": days})
    expected: set[date] = set()
    if parsed:
        cursor = parsed[0]
        while cursor <= parsed[-1]:
            if cursor.weekday() not in {4, 5}:
                expected.add(cursor)
            cursor += timedelta(days=1)
    missing = sorted(item.isoformat() for item in expected - set(parsed))
    return {
        "date_count": len(parsed),
        "start_date": parsed[0].isoformat() if parsed else None,
        "end_date": parsed[-1].isoformat() if parsed else None,
        "weekday_distribution": dict(weekdays),
        "unexpected_weekend_rows": weekend,
        "missing_observed_market_dates": missing,
        "long_gaps": long_gaps,
        "authoritative": False,
        "official_calendar_comparison": "blocked_pending_human_verified_calendar",
    }


def source_quality_score(
    *,
    schema_complete: bool,
    duplicate_rate: float,
    invalid_rate: float,
    conflict_rate: float,
    date_coverage_rate: float,
    symbol_coverage_rate: float | None,
    adjustment_status: str,
    license_note: str,
    timestamp_trust: str,
    reproducible: bool,
    agreement_rate: float | None,
) -> dict[str, Any]:
    components = {
        "schema_completeness": 100.0 if schema_complete else 0.0,
        "duplicate_rate": max(0.0, 100.0 * (1.0 - duplicate_rate / 0.10)),
        "invalid_row_rate": max(0.0, 100.0 * (1.0 - invalid_rate / 0.05)),
        "conflict_rate": max(0.0, 100.0 * (1.0 - conflict_rate / 0.25)),
        "date_coverage": min(100.0, max(0.0, date_coverage_rate * 100.0)),
        "symbol_coverage": 50.0
        if symbol_coverage_rate is None
        else min(100.0, max(0.0, symbol_coverage_rate * 100.0)),
        "adjustment_documentation": {"adjusted": 100.0, "unadjusted": 100.0, "mixed": 75.0}.get(
            adjustment_status, 20.0
        ),
        "licensing_clarity": 100.0
        if "CC BY" in license_note
        else 25.0
        if "no explicit" in license_note.lower()
        else 50.0,
        "timestamp_provenance": {
            "exchange_verified": 100.0,
            "licensed_vendor": 75.0,
            "provider_asserted": 40.0,
            "operator_attested": 25.0,
            "receipt_only": 10.0,
            "unknown": 0.0,
        }.get(timestamp_trust, 0.0),
        "reproducibility": 100.0 if reproducible else 0.0,
        "cross_source_agreement": 50.0 if agreement_rate is None else agreement_rate * 100.0,
    }
    weighted = sum(components[name] * QUALITY_WEIGHTS[name] for name in QUALITY_WEIGHTS) / 100
    return {
        "score": round(weighted, 2),
        "components": {name: round(value, 2) for name, value in components.items()},
        "weights": QUALITY_WEIGHTS,
        "truth_established": False,
        "use": "review_priority_only",
    }


class CanonicalCandidateBuilder:
    def __init__(self, database_path: Path, output_dir: Path, *, tolerance: Decimal) -> None:
        self.database_path = database_path
        self.output_dir = output_dir
        self.tolerance = tolerance
        output_dir.mkdir(parents=True, exist_ok=True)
        database_path.unlink(missing_ok=True)
        self.db = sqlite3.connect(database_path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self._create_schema()
        self.inventories: dict[str, InventoryAccumulator] = {}
        self.mappings: dict[tuple[str, str], SymbolMapping] = {}

    def _create_schema(self) -> None:
        self.db.executescript(
            """
            CREATE TABLE observations (
              id INTEGER PRIMARY KEY, source_dataset_id TEXT NOT NULL, source_hash TEXT NOT NULL,
              source_name TEXT NOT NULL, source_row_id TEXT NOT NULL, original_symbol TEXT NOT NULL,
              normalized_symbol TEXT NOT NULL, raw_date TEXT NOT NULL, trading_date TEXT,
              raw_open TEXT, raw_high TEXT, raw_low TEXT, raw_close TEXT, raw_volume TEXT,
              open TEXT, high TEXT, low TEXT, close TEXT, volume TEXT,
              adjustment_status TEXT NOT NULL, invalid_categories TEXT NOT NULL, warnings TEXT NOT NULL,
              accepted_for_candidate INTEGER NOT NULL, value_fingerprint TEXT NOT NULL,
              mapping_reason TEXT NOT NULL, mapping_confidence TEXT NOT NULL,
              mapping_approval_status TEXT NOT NULL, instrument_class TEXT NOT NULL,
              transformation_version TEXT NOT NULL, transformation_reason TEXT NOT NULL,
              UNIQUE(source_dataset_id, source_row_id, adjustment_status)
            );
            CREATE INDEX idx_observation_key ON observations(normalized_symbol,trading_date,adjustment_status);
            CREATE INDEX idx_observation_source ON observations(source_dataset_id);
            CREATE TABLE symbol_mappings (
              original_symbol TEXT, normalized_symbol TEXT, mapping_reason TEXT, instrument_class TEXT,
              confidence TEXT, evidence_source TEXT, approval_status TEXT, effective_from TEXT,
              effective_to TEXT, PRIMARY KEY(original_symbol,evidence_source)
            );
            CREATE TABLE duplicate_groups (
              normalized_symbol TEXT, trading_date TEXT, adjustment_status TEXT, source_dataset_id TEXT,
              source_name TEXT, row_count INTEGER, duplicate_type TEXT, resolution_status TEXT, source_row_ids TEXT,
              value_fingerprints TEXT, final_review_status TEXT
            );
            CREATE TABLE cross_source_comparisons (
              normalized_symbol TEXT, trading_date TEXT, source_a TEXT, source_b TEXT,
              source_name_a TEXT, source_name_b TEXT,
              adjustment_a TEXT, adjustment_b TEXT, values_a TEXT, values_b TEXT,
              absolute_differences TEXT, percentage_differences TEXT, volume_difference TEXT,
              possible_corporate_action TEXT, tolerance_result TEXT, preferred_source_recommendation TEXT,
              evidence_quality TEXT, final_review_status TEXT
            );
            CREATE TABLE corporate_action_candidates (
              normalized_symbol TEXT, trading_date TEXT, source_dataset_id TEXT,
              candidate_type TEXT, previous_close TEXT, current_close TEXT, adjusted_close TEXT,
              unadjusted_close TEXT, volume_change TEXT, evidence TEXT, review_status TEXT
            );
            CREATE TABLE canonical_candidates (
              id INTEGER PRIMARY KEY, normalized_symbol TEXT, trading_date TEXT, open TEXT, high TEXT,
              low TEXT, close TEXT, volume TEXT, adjustment_status TEXT, selected_source TEXT,
              contributing_source_ids TEXT, lineage TEXT, quality_status TEXT, confidence TEXT,
              transformation_version TEXT, review_status TEXT,
              UNIQUE(normalized_symbol,trading_date,adjustment_status)
            );
            """
        )

    def ingest_rows(
        self,
        source: DatasetSource,
        rows: Iterable[tuple[str, dict[str, str]]],
        fieldnames: Sequence[str],
    ) -> None:
        mapping = infer_mapping(fieldnames)
        accumulator = InventoryAccumulator(source=source)
        if set(mapping) != set(REQUIRED_FIELDS):
            accumulator.schema_inconsistencies += 1
        batch: list[tuple[Any, ...]] = []
        for row_id, row in rows:
            observation, symbol_mapping = parse_observation(source, row_id, row, mapping)
            accumulator.observed_rows += 1
            if observation.original_symbol:
                accumulator.symbols.add(observation.original_symbol.strip().upper())
            if observation.trading_date:
                accumulator.dates.add(observation.trading_date)
            accumulator.categories.update(observation.invalid_categories)
            accumulator.warnings.update(observation.warnings)
            self.mappings[(symbol_mapping.original_symbol, source.source_name)] = symbol_mapping
            batch.append(
                (
                    observation.source_dataset_id,
                    observation.source_hash,
                    observation.source_name,
                    observation.source_row_id,
                    observation.original_symbol,
                    observation.normalized_symbol,
                    observation.raw_date,
                    observation.trading_date,
                    observation.raw_open,
                    observation.raw_high,
                    observation.raw_low,
                    observation.raw_close,
                    observation.raw_volume,
                    observation.open,
                    observation.high,
                    observation.low,
                    observation.close,
                    observation.volume,
                    observation.adjustment_status,
                    json.dumps(observation.invalid_categories),
                    json.dumps(observation.warnings),
                    int(observation.accepted_for_candidate),
                    observation.value_fingerprint,
                    observation.mapping_reason,
                    observation.mapping_confidence,
                    observation.mapping_approval_status,
                    observation.instrument_class,
                    observation.transformation_version,
                    observation.transformation_reason,
                )
            )
            if len(batch) >= 10_000:
                self._insert_observations(batch)
                batch.clear()
        if batch:
            self._insert_observations(batch)
        self.inventories[source.logical_name or source.source_name] = accumulator
        self.db.commit()

    def _insert_observations(self, batch: list[tuple[Any, ...]]) -> None:
        self.db.executemany(
            """
            INSERT INTO observations (
              source_dataset_id,source_hash,source_name,source_row_id,original_symbol,normalized_symbol,
              raw_date,trading_date,raw_open,raw_high,raw_low,raw_close,raw_volume,open,high,low,close,
              volume,adjustment_status,invalid_categories,warnings,accepted_for_candidate,
              value_fingerprint,mapping_reason,mapping_confidence,mapping_approval_status,
              instrument_class,transformation_version,transformation_reason
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            batch,
        )

    def materialize_symbol_mappings(self) -> None:
        self.db.executemany(
            "INSERT INTO symbol_mappings VALUES (?,?,?,?,?,?,?,?,?)",
            [
                (
                    item.original_symbol,
                    item.normalized_symbol,
                    item.mapping_reason,
                    item.instrument_class,
                    item.confidence,
                    item.evidence_source,
                    item.approval_status,
                    item.effective_from,
                    item.effective_to,
                )
                for item in self.mappings.values()
            ],
        )
        self.db.commit()

    @staticmethod
    def _row_to_observation(row: sqlite3.Row) -> ParsedObservation:
        return ParsedObservation(
            source_dataset_id=row["source_dataset_id"],
            source_hash=row["source_hash"],
            source_name=row["source_name"],
            source_row_id=row["source_row_id"],
            original_symbol=row["original_symbol"],
            normalized_symbol=row["normalized_symbol"],
            raw_date=row["raw_date"],
            trading_date=row["trading_date"],
            raw_open=row["raw_open"],
            raw_high=row["raw_high"],
            raw_low=row["raw_low"],
            raw_close=row["raw_close"],
            raw_volume=row["raw_volume"],
            open=row["open"],
            high=row["high"],
            low=row["low"],
            close=row["close"],
            volume=row["volume"],
            adjustment_status=row["adjustment_status"],
            invalid_categories=tuple(json.loads(row["invalid_categories"])),
            warnings=tuple(json.loads(row["warnings"])),
            mapping_reason=row["mapping_reason"],
            mapping_confidence=row["mapping_confidence"],
            mapping_approval_status=row["mapping_approval_status"],
            instrument_class=row["instrument_class"],
            transformation_version=row["transformation_version"],
            transformation_reason=row["transformation_reason"],
        )

    def analyze_duplicates(self) -> Counter[str]:
        self.db.row_factory = sqlite3.Row
        cursor = self.db.execute(
            """
            SELECT * FROM observations WHERE trading_date IS NOT NULL AND normalized_symbol <> ''
            ORDER BY source_dataset_id,normalized_symbol,trading_date,adjustment_status,id
            """
        )
        counts: Counter[str] = Counter()

        def key(row: sqlite3.Row) -> tuple[str, str, str, str]:
            return (
                row["source_dataset_id"],
                row["normalized_symbol"],
                row["trading_date"],
                row["adjustment_status"],
            )

        inserts: list[tuple[Any, ...]] = []
        for group_key, raw_group in groupby(cursor, key=key):
            rows = [self._row_to_observation(row) for row in raw_group]
            if len(rows) < 2:
                continue
            duplicate_type, resolution = duplicate_classification(rows)
            counts[duplicate_type] += 1
            inserts.append(
                (
                    group_key[1],
                    group_key[2],
                    group_key[3],
                    group_key[0],
                    rows[0].source_name,
                    len(rows),
                    duplicate_type,
                    resolution,
                    json.dumps([row.source_row_id for row in rows]),
                    json.dumps(sorted({row.value_fingerprint for row in rows})),
                    "candidate" if resolution == "safe_exact_deduplication" else "under_review",
                )
            )
        self.db.executemany("INSERT INTO duplicate_groups VALUES (?,?,?,?,?,?,?,?,?,?,?)", inserts)
        self.db.commit()
        return counts

    def reconcile_sources(self) -> Counter[str]:
        self.db.row_factory = sqlite3.Row
        cursor = self.db.execute(
            """
            SELECT * FROM observations
            WHERE accepted_for_candidate=1 AND trading_date IS NOT NULL AND normalized_symbol <> ''
            ORDER BY normalized_symbol,trading_date,id
            """
        )

        def key(row: sqlite3.Row) -> tuple[str, str]:
            return (row["normalized_symbol"], row["trading_date"])

        counts: Counter[str] = Counter()
        inserts: list[tuple[Any, ...]] = []
        for group_key, raw_group in groupby(cursor, key=key):
            observations = [self._row_to_observation(row) for row in raw_group]
            representatives: dict[tuple[str, str, str], ParsedObservation] = {}
            for item in observations:
                representatives.setdefault(
                    (item.source_dataset_id, item.adjustment_status, item.value_fingerprint), item
                )
            values = list(representatives.values())
            for index, left in enumerate(values):
                for right in values[index + 1 :]:
                    if left.source_dataset_id == right.source_dataset_id and (
                        left.adjustment_status == right.adjustment_status
                    ):
                        continue
                    result = compare_values(left, right, self.tolerance)
                    status = "exact_match" if result["exact_match"] else result["tolerance_result"]
                    counts[status] += 1
                    corporate = (
                        "adjustment_difference_requires_review"
                        if left.adjustment_status != right.adjustment_status
                        and not result["exact_match"]
                        else "none_observed"
                    )
                    inserts.append(
                        (
                            group_key[0],
                            group_key[1],
                            left.source_dataset_id,
                            right.source_dataset_id,
                            left.source_name,
                            right.source_name,
                            left.adjustment_status,
                            right.adjustment_status,
                            json.dumps(
                                {
                                    f: getattr(left, f)
                                    for f in ("open", "high", "low", "close", "volume")
                                }
                            ),
                            json.dumps(
                                {
                                    f: getattr(right, f)
                                    for f in ("open", "high", "low", "close", "volume")
                                }
                            ),
                            json.dumps(result["absolute_differences"]),
                            json.dumps(result["percentage_differences"]),
                            result["volume_difference"],
                            corporate,
                            result["tolerance_result"],
                            "manual_review_required",
                            "third_party_sources_not_proven_independent",
                            "review_candidate" if result["exact_match"] else "unresolved",
                        )
                    )
                    if len(inserts) >= 10_000:
                        self.db.executemany(
                            "INSERT INTO cross_source_comparisons VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            inserts,
                        )
                        inserts.clear()
        if inserts:
            self.db.executemany(
                "INSERT INTO cross_source_comparisons VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                inserts,
            )
        self.db.commit()
        return counts

    def detect_corporate_actions(self) -> Counter[str]:
        self.db.row_factory = sqlite3.Row
        counts: Counter[str] = Counter({name: 0 for name in CORPORATE_ACTION_CLASSIFICATIONS})
        inserts: list[tuple[Any, ...]] = []
        cursor = self.db.execute(
            """
            SELECT * FROM observations WHERE accepted_for_candidate=1 AND trading_date IS NOT NULL
            AND close IS NOT NULL ORDER BY source_dataset_id,normalized_symbol,adjustment_status,trading_date,id
            """
        )

        def key(row: sqlite3.Row) -> tuple[str, str, str]:
            return (
                row["source_dataset_id"],
                row["normalized_symbol"],
                row["adjustment_status"],
            )

        for group_key, raw_group in groupby(cursor, key=key):
            rows = [self._row_to_observation(row) for row in raw_group]
            unique_by_date: dict[str, ParsedObservation] = {}
            for row in rows:
                if row.trading_date:
                    unique_by_date.setdefault(row.trading_date, row)
            ordered = sorted(unique_by_date.values(), key=lambda item: item.trading_date or "")
            for previous, current in zip(ordered, ordered[1:], strict=False):
                previous_close, current_close = (
                    Decimal(previous.close or "0"),
                    Decimal(current.close or "0"),
                )
                gap_days = (
                    date.fromisoformat(current.trading_date or "")
                    - date.fromisoformat(previous.trading_date or "")
                ).days
                classification = corporate_action_classification(
                    previous_close, current_close, gap_days=gap_days
                )
                if classification == "ordinary_market_movement":
                    continue
                counts[classification] += 1
                volume_change = None
                if previous.volume and current.volume and Decimal(previous.volume) != 0:
                    volume_change = str(Decimal(current.volume) / Decimal(previous.volume) - 1)
                inserts.append(
                    (
                        group_key[1],
                        current.trading_date,
                        group_key[0],
                        classification,
                        previous.close,
                        current.close,
                        None,
                        None,
                        volume_change,
                        json.dumps([previous.source_row_id, current.source_row_id]),
                        "under_review",
                    )
                )
                if len(inserts) >= 10_000:
                    self.db.executemany(
                        "INSERT INTO corporate_action_candidates VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        inserts,
                    )
                    inserts.clear()
        if inserts:
            self.db.executemany(
                "INSERT INTO corporate_action_candidates VALUES (?,?,?,?,?,?,?,?,?,?,?)", inserts
            )
        self.db.commit()
        return counts

    def build_canonical_candidates(self) -> Counter[str]:
        self.db.row_factory = sqlite3.Row
        cursor = self.db.execute(
            """
            SELECT * FROM observations WHERE accepted_for_candidate=1 AND trading_date IS NOT NULL
            AND normalized_symbol <> '' ORDER BY normalized_symbol,trading_date,adjustment_status,id
            """
        )

        def key(row: sqlite3.Row) -> tuple[str, str, str]:
            return (row["normalized_symbol"], row["trading_date"], row["adjustment_status"])

        counts: Counter[str] = Counter()
        inserts: list[tuple[Any, ...]] = []
        for group_key, raw_group in groupby(cursor, key=key):
            rows = [self._row_to_observation(row) for row in raw_group]
            if any(row.mapping_approval_status == "under_review" for row in rows):
                counts["held_for_review"] += 1
                continue
            fingerprints = {row.value_fingerprint for row in rows}
            if len(fingerprints) != 1:
                counts["rejected_conflicting"] += 1
                continue
            source_ids = sorted({row.source_dataset_id for row in rows})
            if len(source_ids) > 1:
                quality_status = "accepted_candidate"
                confidence = "medium"
            elif len(rows) > 1:
                quality_status = "accepted_exact_deduplicated"
                confidence = "medium"
            else:
                quality_status = "accepted_single_source_low_confidence"
                confidence = "low"
            representative = rows[0]
            lineage = [
                {
                    "source_dataset_id": row.source_dataset_id,
                    "source_file_hash": row.source_hash,
                    "source_row_identifier": row.source_row_id,
                    "original_raw_values": {
                        "symbol": row.original_symbol,
                        "date": row.raw_date,
                        "open": row.raw_open,
                        "high": row.raw_high,
                        "low": row.raw_low,
                        "close": row.raw_close,
                        "volume": row.raw_volume,
                    },
                    "transformation_version": row.transformation_version,
                    "transformation_reason": row.transformation_reason,
                }
                for row in rows
            ]
            inserts.append(
                (
                    group_key[0],
                    group_key[1],
                    representative.open,
                    representative.high,
                    representative.low,
                    representative.close,
                    representative.volume,
                    group_key[2],
                    representative.source_dataset_id,
                    json.dumps(source_ids),
                    json.dumps(lineage, separators=(",", ":")),
                    quality_status,
                    confidence,
                    TRANSFORMATION_VERSION,
                    "pending_human_approval",
                )
            )
            counts[quality_status] += 1
            if len(inserts) >= 10_000:
                self.db.executemany(
                    """INSERT INTO canonical_candidates (
                    normalized_symbol,trading_date,open,high,low,close,volume,adjustment_status,
                    selected_source,contributing_source_ids,lineage,quality_status,confidence,
                    transformation_version,review_status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    inserts,
                )
                inserts.clear()
        if inserts:
            self.db.executemany(
                """INSERT INTO canonical_candidates (
                normalized_symbol,trading_date,open,high,low,close,volume,adjustment_status,
                selected_source,contributing_source_ids,lineage,quality_status,confidence,
                transformation_version,review_status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                inserts,
            )
        self.db.commit()
        return counts

    def inventory_report(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for logical_name, accumulator in self.inventories.items():
            source = accumulator.source
            duplicate_rows = int(
                self.db.execute(
                    """SELECT COALESCE(SUM(row_count-1),0) FROM duplicate_groups
                    WHERE source_dataset_id=? AND source_name=?""",
                    (source.dataset_id, source.source_name),
                ).fetchone()[0]
            )
            result.append(
                {
                    "logical_name": logical_name,
                    "dataset_id": source.dataset_id,
                    "source_name": source.source_name,
                    "source_hash": source.source_hash,
                    "observed_row_count": accumulator.observed_rows,
                    "stated_row_count": source.stated_row_count,
                    "row_count_discrepancy": (
                        accumulator.observed_rows - source.stated_row_count
                        if source.stated_row_count is not None
                        else None
                    ),
                    "unique_symbols": len(accumulator.symbols),
                    "stated_symbol_count": source.stated_symbol_count,
                    "stated_symbol_claim": source.stated_symbol_claim,
                    "observed_start_date": min(accumulator.dates, default=None),
                    "observed_end_date": max(accumulator.dates, default=None),
                    "duplicate_row_count": duplicate_rows,
                    "invalid_ohlc_count": sum(
                        accumulator.categories[name]
                        for name in ("high_below_low", "open_outside_range", "close_outside_range")
                    ),
                    "invalid_volume_count": accumulator.categories["negative_volume"],
                    "missing_symbol_count": accumulator.categories["symbol_missing"],
                    "missing_date_count": accumulator.categories["impossible_date"],
                    "zero_price_count": accumulator.categories["zero_price"],
                    "zero_volume_count": accumulator.warnings["zero_volume"],
                    "non_trading_day_rows": accumulator.warnings["non_trading_day_observed"],
                    "schema_inconsistencies": accumulator.schema_inconsistencies,
                    "encoding_issues": accumulator.encoding_issues,
                    "numeric_parsing_failures": accumulator.categories["non_numeric_value"],
                    "adjustment_status": source.adjustment_status,
                    "license_status": source.license_note,
                    "source_trust": source.source_trust,
                    "timestamp_trust": source.timestamp_trust,
                    "category_counts": dict(accumulator.categories),
                }
            )
        return result

    def close(self) -> None:
        self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self.db.close()


def csv_rows(
    handle: TextIO, *, row_prefix: str
) -> tuple[list[str], Iterator[tuple[str, dict[str, str]]]]:
    reader = csv.DictReader(handle)
    if not reader.fieldnames:
        raise ValueError("Dataset has no CSV fields")
    fieldnames = list(reader.fieldnames)
    mapping = infer_mapping(fieldnames)
    if len(mapping) != len(REQUIRED_FIELDS) and len(fieldnames) == 7:
        handle.seek(0)
        fieldnames = ["symbol", "date", "open", "high", "low", "close", "volume"]
        reader = csv.DictReader(handle, fieldnames=fieldnames)

    def iterator() -> Iterator[tuple[str, dict[str, str]]]:
        for number, row in enumerate(reader, 2):
            yield (
                f"{row_prefix}:{number}",
                {str(key): str(value or "") for key, value in row.items()},
            )

    return fieldnames, iterator()


def write_csv(path: Path, rows: Iterable[dict[str, Any]], fieldnames: Sequence[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def review_html(summary: dict[str, Any]) -> str:
    from app.services.report_provenance import html_provenance

    inventories = "".join(
        f"<tr><td>{escape(str(row['logical_name']))}</td><td>{row['observed_row_count']}</td>"
        f"<td>{row['unique_symbols']}</td><td>{row['duplicate_row_count']}</td>"
        f"<td>{row['invalid_ohlc_count']}</td></tr>"
        for row in summary["dataset_inventory"]
    )
    return (
        "<!doctype html><html><head><meta charset='utf-8'><title>DSE candidate review</title></head>"
        "<body><h1>Canonical DSE research candidate - human review</h1>"
        "<p><strong>INACTIVE - HUMAN APPROVAL REQUIRED - QUALIFICATION 0/60</strong></p>"
        f"{html_provenance(summary['provenance'])}"
        "<table><tr><th>Dataset</th><th>Rows</th><th>Symbols</th><th>Duplicates</th>"
        f"<th>Invalid OHLC</th></tr>{inventories}</table>"
        f"<pre>{escape(json.dumps(summary['canonical_candidate_counts'], indent=2))}</pre>"
        "<p>No dataset, rule, strategy, campaign, session, proposal, order, or fill was activated.</p>"
        "</body></html>"
    )


def mapping_rows(builder: CanonicalCandidateBuilder) -> list[dict[str, Any]]:
    return [
        asdict(item)
        for item in sorted(
            builder.mappings.values(),
            key=lambda row: (row.normalized_symbol, row.original_symbol, row.evidence_source),
        )
    ]
