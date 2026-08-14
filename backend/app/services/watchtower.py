from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from app.services.forward_paper_validation import _candidate_tables

WATCHTOWER_SCHEMA = "dse_watchtower@0.1.0"
REPORT_LABELS = {
    "NORMAL",
    "WATCH",
    "HIGH_ATTENTION",
    "DATA_ISSUE",
    "INSUFFICIENT_HISTORY",
}
INSTRUMENT_MASTER_COLUMNS = (
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
EVENT_FIELDS = (
    "trading_code",
    "event_type",
    "event_time",
    "publication_time",
    "observed_at",
    "source_tier",
    "source_reference",
    "short_factual_summary",
    "contradiction_flag",
)
DAY_END_COLUMNS = {
    "date": "DATE",
    "trading_code": "TRADING CODE",
    "ltp": "LTP*",
    "high": "HIGH",
    "low": "LOW",
    "open": "OPENP*",
    "close": "CLOSEP*",
    "ycp": "YCP",
    "trade_count": "TRADE",
    "traded_value_mn": "VALUE (mn)",
    "volume": "VOLUME",
}
SYMBOL_PATTERN = re.compile(r"^[A-Z0-9().&_-]{1,32}$")
TRAILING_WINDOW = 60
MINIMUM_HISTORY = 40
DHaka = ZoneInfo("Asia/Dhaka")


class WatchtowerError(RuntimeError):
    """A fail-closed local Watchtower input or output error."""


class VerificationStatus(StrEnum):
    VERIFIED_EQUITY = "VERIFIED_EQUITY"
    UNVERIFIED_INSTRUMENT = "UNVERIFIED_INSTRUMENT"
    NON_EQUITY = "NON_EQUITY"


class DataStatus(StrEnum):
    USABLE = "USABLE"
    ZERO_ACTIVITY = "ZERO_ACTIVITY"
    DATA_ISSUE = "DATA_ISSUE"


class FeatureStatus(StrEnum):
    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"
    INSUFFICIENT_HISTORY = "INSUFFICIENT_HISTORY"


FeatureScalar = Decimal | str


@dataclass(frozen=True)
class Feature:
    status: FeatureStatus
    value: FeatureScalar | None = None
    unit: str | None = None
    reason: str | None = None

    def payload(self) -> dict[str, str | None]:
        value = _decimal_text(self.value) if isinstance(self.value, Decimal) else self.value
        return {
            "status": self.status.value,
            "value": value,
            "unit": self.unit,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class MarketObservation:
    market_date: date
    trading_code: str
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    ltp: Decimal
    ycp: Decimal
    volume: int
    trade_count: int
    traded_value_mn: Decimal
    data_status: DataStatus
    unavailable_reason: str | None


@dataclass(frozen=True)
class DayEndSession:
    market_date: date
    source_path: Path
    source_sha256: str
    observations: tuple[MarketObservation, ...]


@dataclass(frozen=True)
class InstrumentMetadata:
    trading_code: str
    company_name: str
    sector: str
    instrument_type: str
    market_category: str
    listing_status: str
    observed_at: str
    source_reference: str
    verification_status: VerificationStatus


@dataclass(frozen=True)
class EventEvidence:
    trading_code: str
    event_type: str
    event_time: datetime
    publication_time: datetime
    observed_at: datetime
    source_tier: str
    source_reference: str
    short_factual_summary: str
    contradiction_flag: bool

    @property
    def available_at(self) -> datetime:
        return max(self.publication_time, self.observed_at)


def _decimal_text(value: Decimal, *, places: int = 6) -> str:
    quantum = Decimal(1).scaleb(-places)
    rounded = value.quantize(quantum, rounding=ROUND_HALF_EVEN)
    text = format(rounded, "f").rstrip("0").rstrip(".")
    return text or "0"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _resolve_local_file(path: Path, *, suffixes: set[str]) -> Path:
    text = str(path)
    if "://" in text or text.startswith(("\\\\", "//")) or path.is_symlink():
        raise WatchtowerError("Watchtower accepts regular local files only")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise WatchtowerError(f"Local input does not exist: {path}") from exc
    if not resolved.is_file() or resolved.suffix.lower() not in suffixes:
        raise WatchtowerError(f"Unsupported local input: {path}")
    return resolved


def _parse_decimal(value: str, *, field: str, row_number: int) -> Decimal:
    try:
        parsed = Decimal(value.replace(",", "").strip())
    except InvalidOperation as exc:
        raise WatchtowerError(f"Invalid {field} on DSE row {row_number}") from exc
    if not parsed.is_finite() or parsed < 0:
        raise WatchtowerError(f"Invalid {field} on DSE row {row_number}")
    return parsed


def _parse_nonnegative_int(value: str, *, field: str, row_number: int) -> int:
    try:
        parsed = int(value.replace(",", "").strip())
    except ValueError as exc:
        raise WatchtowerError(f"Invalid {field} on DSE row {row_number}") from exc
    if parsed < 0:
        raise WatchtowerError(f"Invalid {field} on DSE row {row_number}")
    return parsed


def _observation_status(
    *,
    open_price: Decimal,
    high: Decimal,
    low: Decimal,
    close: Decimal,
    ltp: Decimal,
    ycp: Decimal,
    volume: int,
    trade_count: int,
    traded_value_mn: Decimal,
) -> tuple[DataStatus, str | None]:
    activity_zero = (volume == 0, trade_count == 0, traded_value_mn == 0)
    if all(activity_zero):
        return DataStatus.ZERO_ACTIVITY, "source_reports_zero_activity"
    if any(activity_zero):
        return DataStatus.DATA_ISSUE, "inconsistent_activity_fields"
    if min(open_price, high, low, close, ltp, ycp) <= 0:
        return DataStatus.DATA_ISSUE, "nonpositive_price_on_active_row"
    if not low <= min(open_price, close, ltp) <= max(open_price, close, ltp) <= high:
        return DataStatus.DATA_ISSUE, "malformed_ohlc_or_ltp"
    return DataStatus.USABLE, None


def _parse_day_end_table(rows: list[list[str]]) -> tuple[date, tuple[MarketObservation, ...]] | None:
    required = set(DAY_END_COLUMNS.values())
    header_index = next((index for index, row in enumerate(rows) if required.issubset(row)), None)
    if header_index is None:
        raise WatchtowerError("Official DSE Day End columns are missing")
    header = rows[header_index]
    if len(header) != len(set(header)):
        raise WatchtowerError("Official DSE Day End header contains duplicate columns")
    positions = {key: header.index(value) for key, value in DAY_END_COLUMNS.items()}
    observations: list[MarketObservation] = []
    symbols: set[str] = set()
    dates: set[date] = set()
    for row_number, row in enumerate(rows[header_index + 1 :], header_index + 2):
        if not any(cell.strip() for cell in row):
            continue
        if len(row) < len(header):
            raise WatchtowerError(f"Malformed DSE row {row_number}")
        try:
            market_date = date.fromisoformat(row[positions["date"]].strip())
        except ValueError as exc:
            raise WatchtowerError(f"Invalid market date on DSE row {row_number}") from exc
        trading_code = row[positions["trading_code"]].strip().upper()
        if not SYMBOL_PATTERN.fullmatch(trading_code):
            raise WatchtowerError(f"Invalid trading code on DSE row {row_number}")
        if trading_code in symbols:
            raise WatchtowerError(f"Duplicate DSE trading code: {trading_code}")
        symbols.add(trading_code)
        dates.add(market_date)
        open_price = _parse_decimal(row[positions["open"]], field="open", row_number=row_number)
        high = _parse_decimal(row[positions["high"]], field="high", row_number=row_number)
        low = _parse_decimal(row[positions["low"]], field="low", row_number=row_number)
        close = _parse_decimal(row[positions["close"]], field="close", row_number=row_number)
        ltp = _parse_decimal(row[positions["ltp"]], field="LTP", row_number=row_number)
        ycp = _parse_decimal(row[positions["ycp"]], field="YCP", row_number=row_number)
        volume = _parse_nonnegative_int(
            row[positions["volume"]], field="volume", row_number=row_number
        )
        trade_count = _parse_nonnegative_int(
            row[positions["trade_count"]], field="trade count", row_number=row_number
        )
        traded_value_mn = _parse_decimal(
            row[positions["traded_value_mn"]], field="traded value", row_number=row_number
        )
        status, reason = _observation_status(
            open_price=open_price,
            high=high,
            low=low,
            close=close,
            ltp=ltp,
            ycp=ycp,
            volume=volume,
            trade_count=trade_count,
            traded_value_mn=traded_value_mn,
        )
        observations.append(
            MarketObservation(
                market_date=market_date,
                trading_code=trading_code,
                open=open_price,
                high=high,
                low=low,
                close=close,
                ltp=ltp,
                ycp=ycp,
                volume=volume,
                trade_count=trade_count,
                traded_value_mn=traded_value_mn,
                data_status=status,
                unavailable_reason=reason,
            )
        )
    if not observations:
        return None
    if len(dates) != 1:
        raise WatchtowerError("A Watchtower Day End file must contain exactly one market date")
    return next(iter(dates)), tuple(sorted(observations, key=lambda item: item.trading_code))


def parse_day_end_file(path: Path) -> DayEndSession:
    resolved = _resolve_local_file(path, suffixes={".csv", ".htm", ".html"})
    raw = resolved.read_bytes()
    try:
        tables = _candidate_tables(raw, resolved.suffix.lower())
    except RuntimeError as exc:
        raise WatchtowerError(str(exc)) from exc
    populated = [parsed for rows in tables if (parsed := _parse_day_end_table(rows)) is not None]
    if not populated:
        raise WatchtowerError("Official DSE Day End file contains no observations")
    if len(populated) != 1:
        raise WatchtowerError("Official DSE Day End HTML contains ambiguous populated tables")
    market_date, observations = populated[0]
    return DayEndSession(
        market_date=market_date,
        source_path=resolved,
        source_sha256=hashlib.sha256(raw).hexdigest(),
        observations=observations,
    )


def load_day_end_sessions(day_end_directory: Path) -> tuple[DayEndSession, ...]:
    try:
        directory = day_end_directory.resolve(strict=True)
    except OSError as exc:
        raise WatchtowerError("Day End directory does not exist") from exc
    if not directory.is_dir() or directory.is_symlink():
        raise WatchtowerError("Day End input must be a regular local directory")
    paths = sorted(
        path
        for path in directory.iterdir()
        if path.is_file() and path.suffix.lower() in {".csv", ".htm", ".html"}
    )
    if not paths:
        raise WatchtowerError("No local DSE Day End files were found")
    sessions = sorted((parse_day_end_file(path) for path in paths), key=lambda item: item.market_date)
    seen: dict[date, str] = {}
    unique: list[DayEndSession] = []
    for session in sessions:
        prior_hash = seen.get(session.market_date)
        if prior_hash is not None and prior_hash != session.source_sha256:
            raise WatchtowerError(
                f"Conflicting local Day End files exist for {session.market_date.isoformat()}"
            )
        if prior_hash is None:
            seen[session.market_date] = session.source_sha256
            unique.append(session)
    return tuple(unique)


def _parse_aware_datetime(value: str, *, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError as exc:
        raise WatchtowerError(f"Invalid {field}: {value}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise WatchtowerError(f"{field} must include a UTC offset")
    return parsed


def load_instrument_master(path: Path | None) -> dict[str, InstrumentMetadata]:
    if path is None or not path.exists():
        return {}
    resolved = _resolve_local_file(path, suffixes={".csv"})
    with resolved.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None or tuple(reader.fieldnames) != INSTRUMENT_MASTER_COLUMNS:
            raise WatchtowerError("Instrument master columns do not match the Watchtower contract")
        records: dict[str, InstrumentMetadata] = {}
        for row_number, row in enumerate(reader, 2):
            code = row["trading_code"].strip().upper()
            if not SYMBOL_PATTERN.fullmatch(code):
                raise WatchtowerError(f"Invalid instrument-master code on row {row_number}")
            if code in records:
                raise WatchtowerError(f"Duplicate instrument-master code: {code}")
            try:
                status = VerificationStatus(row["verification_status"].strip().upper())
            except ValueError as exc:
                raise WatchtowerError(
                    f"Invalid verification_status on instrument-master row {row_number}"
                ) from exc
            company_name = row["company_name"].strip()
            sector = row["sector"].strip()
            instrument_type = row["instrument_type"].strip().upper()
            market_category = row["market_category"].strip().upper()
            listing_status = row["listing_status"].strip().upper()
            observed_at = row["observed_at"].strip()
            source_reference = row["source_reference"].strip()
            if status is VerificationStatus.VERIFIED_EQUITY:
                required = (
                    company_name,
                    sector,
                    market_category,
                    listing_status,
                    observed_at,
                    source_reference,
                )
                if instrument_type not in {"EQUITY", "ORDINARY_EQUITY"} or not all(required):
                    raise WatchtowerError(
                        f"VERIFIED_EQUITY row {row_number} lacks required official metadata"
                    )
                _parse_aware_datetime(observed_at, field="instrument observed_at")
            elif status is VerificationStatus.NON_EQUITY:
                if not instrument_type or instrument_type in {"EQUITY", "ORDINARY_EQUITY"}:
                    raise WatchtowerError(
                        f"NON_EQUITY row {row_number} has an invalid instrument_type"
                    )
                if not observed_at or not source_reference:
                    raise WatchtowerError(
                        f"NON_EQUITY row {row_number} lacks verification provenance"
                    )
                _parse_aware_datetime(observed_at, field="instrument observed_at")
            records[code] = InstrumentMetadata(
                trading_code=code,
                company_name=company_name,
                sector=sector,
                instrument_type=instrument_type,
                market_category=market_category,
                listing_status=listing_status,
                observed_at=observed_at,
                source_reference=source_reference,
                verification_status=status,
            )
    return records


def load_event_evidence(path: Path | None) -> tuple[EventEvidence, ...]:
    if path is None or not path.exists():
        return ()
    resolved = _resolve_local_file(path, suffixes={".json"})
    try:
        raw = json.loads(resolved.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise WatchtowerError("Manual event evidence is not valid JSON") from exc
    if not isinstance(raw, list):
        raise WatchtowerError("Manual event evidence must be a JSON array")
    events: list[EventEvidence] = []
    for index, item in enumerate(raw, 1):
        if not isinstance(item, dict) or not set(EVENT_FIELDS).issubset(item):
            raise WatchtowerError(f"Manual event record {index} is incomplete")
        code = str(item["trading_code"]).strip().upper()
        tier = str(item["source_tier"]).strip().upper().removeprefix("TIER ")
        if not SYMBOL_PATTERN.fullmatch(code) or tier not in {"A", "B", "C", "D", "E"}:
            raise WatchtowerError(f"Manual event record {index} has invalid code or tier")
        contradiction = item["contradiction_flag"]
        if not isinstance(contradiction, bool):
            raise WatchtowerError(f"Manual event record {index} contradiction_flag must be boolean")
        summary = str(item["short_factual_summary"]).strip()
        source_reference = str(item["source_reference"]).strip()
        event_type = str(item["event_type"]).strip()
        if not summary or not source_reference or not event_type:
            raise WatchtowerError(f"Manual event record {index} lacks factual provenance")
        events.append(
            EventEvidence(
                trading_code=code,
                event_type=event_type,
                event_time=_parse_aware_datetime(str(item["event_time"]), field="event_time"),
                publication_time=_parse_aware_datetime(
                    str(item["publication_time"]), field="publication_time"
                ),
                observed_at=_parse_aware_datetime(
                    str(item["observed_at"]), field="event observed_at"
                ),
                source_tier=tier,
                source_reference=source_reference,
                short_factual_summary=summary,
                contradiction_flag=contradiction,
            )
        )
    return tuple(
        sorted(events, key=lambda item: (item.trading_code, item.available_at, item.event_type))
    )


def decimal_median(values: Sequence[Decimal]) -> Decimal:
    if not values:
        raise ValueError("median requires at least one value")
    ordered = sorted(values)
    midpoint = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[midpoint]
    return (ordered[midpoint - 1] + ordered[midpoint]) / Decimal(2)


def median_and_mad(values: Sequence[Decimal]) -> tuple[Decimal, Decimal]:
    median = decimal_median(values)
    mad = decimal_median([abs(value - median) for value in values])
    return median, mad


def _available(value: FeatureScalar, unit: str) -> Feature:
    return Feature(FeatureStatus.AVAILABLE, value=value, unit=unit)


def _unavailable(reason: str) -> Feature:
    return Feature(FeatureStatus.UNAVAILABLE, reason=reason)


def _insufficient(actual: int) -> Feature:
    return Feature(
        FeatureStatus.INSUFFICIENT_HISTORY,
        reason=f"requires_{MINIMUM_HISTORY}_valid_prior_sessions;found_{actual}",
    )


def safe_multiple(current: Decimal, history: Sequence[Decimal]) -> Feature:
    if len(history) < MINIMUM_HISTORY:
        return _insufficient(len(history))
    baseline = decimal_median(history[-TRAILING_WINDOW:])
    if baseline == 0:
        return _unavailable("trailing_median_is_zero")
    return _available(current / baseline, "multiple")


def robust_zscore(current: Decimal, history: Sequence[Decimal]) -> Feature:
    if len(history) < MINIMUM_HISTORY:
        return _insufficient(len(history))
    median, mad = median_and_mad(history[-TRAILING_WINDOW:])
    if mad == 0:
        if current == median:
            return _available(Decimal(0), "robust_z")
        return _unavailable("trailing_mad_is_zero")
    return _available(Decimal("0.6745") * (current - median) / mad, "robust_z")


def _percentage(numerator: Decimal, denominator: Decimal) -> Decimal:
    return numerator / denominator * Decimal(100)


def calculate_features(
    current: MarketObservation,
    prior: Sequence[MarketObservation],
) -> dict[str, Feature]:
    names = (
        "daily_return_pct",
        "opening_gap_pct",
        "intraday_range_pct",
        "volume_multiple",
        "trade_count_multiple",
        "traded_value_multiple",
        "robust_return_z",
        "robust_volume_z",
        "volatility_expansion",
        "recent_high_low_breakout",
    )
    if current.data_status is not DataStatus.USABLE:
        return {name: _unavailable(current.unavailable_reason or "current_row_unusable") for name in names}
    daily_return = _percentage(current.close - current.ycp, current.ycp)
    opening_gap = _percentage(current.open - current.ycp, current.ycp)
    intraday_range = _percentage(current.high - current.low, current.ycp)
    eligible = [item for item in prior if item.data_status is DataStatus.USABLE]
    eligible = eligible[-TRAILING_WINDOW:]
    return_history = [_percentage(item.close - item.ycp, item.ycp) for item in eligible]
    volume_history = [Decimal(item.volume) for item in eligible]
    trade_history = [Decimal(item.trade_count) for item in eligible]
    value_history = [item.traded_value_mn for item in eligible]
    features = {
        "daily_return_pct": _available(daily_return, "percent"),
        "opening_gap_pct": _available(opening_gap, "percent"),
        "intraday_range_pct": _available(intraday_range, "percent"),
        "volume_multiple": safe_multiple(Decimal(current.volume), volume_history),
        "trade_count_multiple": safe_multiple(Decimal(current.trade_count), trade_history),
        "traded_value_multiple": safe_multiple(current.traded_value_mn, value_history),
        "robust_return_z": robust_zscore(daily_return, return_history),
        "robust_volume_z": robust_zscore(Decimal(current.volume), volume_history),
        "volatility_expansion": safe_multiple(
            abs(daily_return), [abs(value) for value in return_history]
        ),
    }
    if len(eligible) < MINIMUM_HISTORY:
        features["recent_high_low_breakout"] = _insufficient(len(eligible))
    else:
        prior_high = max(item.high for item in eligible)
        prior_low = min(item.low for item in eligible)
        breakout = "HIGH" if current.close > prior_high else "LOW" if current.close < prior_low else "NONE"
        features["recent_high_low_breakout"] = _available(breakout, "observation")
    return features


def _severity(value: Decimal, thresholds: Sequence[tuple[Decimal, int]]) -> int:
    return max((points for threshold, points in thresholds if value >= threshold), default=0)


def attention_score(features: Mapping[str, Feature]) -> tuple[int, dict[str, int]]:
    components: dict[str, int] = {}

    def numeric(name: str, thresholds: Sequence[tuple[Decimal, int]], *, absolute: bool = False) -> None:
        feature = features[name]
        if feature.status is FeatureStatus.AVAILABLE and isinstance(feature.value, Decimal):
            value = abs(feature.value) if absolute else feature.value
            components[name] = _severity(value, thresholds)
        else:
            components[name] = 0

    numeric(
        "daily_return_pct",
        ((Decimal("5"), 1), (Decimal("7.5"), 2), (Decimal("10"), 3)),
        absolute=True,
    )
    numeric(
        "opening_gap_pct",
        ((Decimal("3"), 1), (Decimal("5"), 2)),
        absolute=True,
    )
    numeric(
        "intraday_range_pct",
        ((Decimal("5"), 1), (Decimal("8"), 2)),
    )
    multiples = ((Decimal("2"), 1), (Decimal("5"), 2), (Decimal("10"), 3))
    numeric("volume_multiple", multiples)
    numeric("trade_count_multiple", multiples)
    numeric("traded_value_multiple", multiples)
    z_thresholds = ((Decimal("3"), 1), (Decimal("4"), 2), (Decimal("5"), 3))
    numeric("robust_return_z", z_thresholds, absolute=True)
    numeric("robust_volume_z", z_thresholds, absolute=True)
    numeric(
        "volatility_expansion",
        ((Decimal("2"), 1), (Decimal("3"), 2), (Decimal("5"), 3)),
    )
    breakout = features["recent_high_low_breakout"]
    components["recent_high_low_breakout"] = (
        1
        if breakout.status is FeatureStatus.AVAILABLE and breakout.value in {"HIGH", "LOW"}
        else 0
    )
    return sum(components.values()), components


def _default_instrument(code: str) -> InstrumentMetadata:
    return InstrumentMetadata(
        trading_code=code,
        company_name="",
        sector="",
        instrument_type="",
        market_category="",
        listing_status="",
        observed_at="",
        source_reference="",
        verification_status=VerificationStatus.UNVERIFIED_INSTRUMENT,
    )


def _event_context(
    code: str,
    events: Sequence[EventEvidence],
    as_of: datetime,
) -> dict[str, Any]:
    matched = [event for event in events if event.trading_code == code and event.available_at <= as_of]
    tiers = {event.source_tier for event in matched}
    if tiers & {"A", "B"}:
        status = "OFFICIAL_EVENT_PRESENT"
    elif tiers & {"C", "D"}:
        status = "CORROBORATING_EVENT_PRESENT"
    elif "E" in tiers:
        status = "RUMOUR_ONLY_INVESTIGATE"
    else:
        status = "NO_EXPLANATION_FOUND"
    return {
        "status": status,
        "causality_inferred": False,
        "contradictory_evidence_present": any(event.contradiction_flag for event in matched),
        "records": [
            {
                "event_type": event.event_type,
                "event_time": event.event_time.isoformat(),
                "publication_time": event.publication_time.isoformat(),
                "observed_at": event.observed_at.isoformat(),
                "available_at": event.available_at.isoformat(),
                "source_tier": event.source_tier,
                "source_reference": event.source_reference,
                "short_factual_summary": event.short_factual_summary,
                "contradiction_flag": event.contradiction_flag,
            }
            for event in matched
        ],
    }


def _feature_value(features: Mapping[str, Feature], name: str) -> Decimal | None:
    feature = features[name]
    return feature.value if feature.status is FeatureStatus.AVAILABLE and isinstance(feature.value, Decimal) else None


def _classify(
    metadata: InstrumentMetadata,
    current: MarketObservation,
    features: Mapping[str, Feature],
    score: int,
) -> tuple[str, bool, list[str]]:
    reasons: list[str] = []
    if metadata.verification_status is VerificationStatus.UNVERIFIED_INSTRUMENT:
        return "DATA_ISSUE", False, ["ordinary_equity_status_not_verified"]
    if metadata.verification_status is VerificationStatus.NON_EQUITY:
        return "NORMAL", False, ["verified_non_equity_not_watchlist_eligible"]
    if metadata.listing_status != "ACTIVE":
        return "DATA_ISSUE", False, ["verified_equity_listing_status_not_active"]
    if current.data_status is not DataStatus.USABLE:
        return "DATA_ISSUE", False, [current.unavailable_reason or "current_row_unusable"]
    if any(feature.status is FeatureStatus.INSUFFICIENT_HISTORY for feature in features.values()):
        return "INSUFFICIENT_HISTORY", False, ["trailing_feature_history_below_minimum"]
    label = "HIGH_ATTENTION" if score >= 8 else "WATCH" if score >= 4 else "NORMAL"
    reasons.extend(
        f"{name}={_decimal_text(feature.value) if isinstance(feature.value, Decimal) else feature.value}"
        for name, feature in features.items()
        if feature.status is FeatureStatus.AVAILABLE and feature.value is not None
    )
    return label, True, reasons or ["no_predeclared_anomaly_threshold_crossed"]


def _instrument_payload(metadata: InstrumentMetadata) -> dict[str, str]:
    return {
        "trading_code": metadata.trading_code,
        "company_name": metadata.company_name,
        "sector": metadata.sector,
        "instrument_type": metadata.instrument_type,
        "market_category": metadata.market_category,
        "listing_status": metadata.listing_status,
        "observed_at": metadata.observed_at,
        "source_reference": metadata.source_reference,
        "verification_status": metadata.verification_status.value,
    }


def _observation_payload(observation: MarketObservation) -> dict[str, Any]:
    return {
        "market_date": observation.market_date.isoformat(),
        "open": _decimal_text(observation.open),
        "high": _decimal_text(observation.high),
        "low": _decimal_text(observation.low),
        "close": _decimal_text(observation.close),
        "ltp": _decimal_text(observation.ltp),
        "ycp": _decimal_text(observation.ycp),
        "volume": observation.volume,
        "trade_count": observation.trade_count,
        "traded_value_mn": _decimal_text(observation.traded_value_mn),
        "data_status": observation.data_status.value,
        "unavailable_reason": observation.unavailable_reason,
    }


def _sector_breadth(records: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: defaultdict[str, list[Decimal]] = defaultdict(list)
    for record in records:
        instrument = record["instrument"]
        if instrument["verification_status"] != VerificationStatus.VERIFIED_EQUITY.value:
            continue
        sector = str(instrument["sector"])
        value = record["features"]["daily_return_pct"]["value"]
        if sector and value is not None:
            grouped[sector].append(Decimal(str(value)))
    results: list[dict[str, Any]] = []
    for sector, returns in sorted(grouped.items()):
        if len(returns) < 3:
            continue
        advancing = sum(value > 0 for value in returns)
        breadth = Decimal(advancing) / Decimal(len(returns))
        median_return = decimal_median(returns)
        direction = (
            "BROAD_STRENGTH"
            if breadth >= Decimal("0.75") and median_return > 0
            else "BROAD_WEAKNESS"
            if breadth <= Decimal("0.25") and median_return < 0
            else "MIXED"
        )
        results.append(
            {
                "sector": sector,
                "verified_observations": len(returns),
                "advancing_share": _decimal_text(breadth),
                "median_return_pct": _decimal_text(median_return),
                "breadth_label": direction,
            }
        )
    return results


def build_watchtower_report(
    sessions: Sequence[DayEndSession],
    instrument_master: Mapping[str, InstrumentMetadata],
    events: Sequence[EventEvidence],
    *,
    protected_database_sha256: str | None = None,
) -> dict[str, Any]:
    if not sessions:
        raise WatchtowerError("Watchtower requires at least one Day End session")
    ordered_sessions = sorted(sessions, key=lambda item: item.market_date)
    latest = ordered_sessions[-1]
    as_of = datetime.combine(latest.market_date, time.max, tzinfo=DHaka)
    by_symbol: defaultdict[str, list[MarketObservation]] = defaultdict(list)
    for session in ordered_sessions:
        for observation in session.observations:
            by_symbol[observation.trading_code].append(observation)
    records: list[dict[str, Any]] = []
    for current in latest.observations:
        history = [item for item in by_symbol[current.trading_code] if item.market_date < latest.market_date]
        features = calculate_features(current, history)
        score, components = attention_score(features)
        metadata = instrument_master.get(current.trading_code, _default_instrument(current.trading_code))
        label, candidate_eligible, reasons = _classify(metadata, current, features, score)
        if label not in REPORT_LABELS:
            raise AssertionError(f"Unsupported Watchtower label: {label}")
        scored_reasons = [
            f"{name}:{points}_point{'s' if points != 1 else ''}"
            for name, points in components.items()
            if points
        ]
        records.append(
            {
                "trading_code": current.trading_code,
                "instrument": _instrument_payload(metadata),
                "market_observation": _observation_payload(current),
                "features": {name: feature.payload() for name, feature in features.items()},
                "attention_score": {
                    "total": score,
                    "components": components,
                    "meaning": "observable_anomaly_attention_only",
                },
                "report_label": label,
                "watchlist_candidate_eligible": candidate_eligible,
                "why_flagged": [*reasons, *scored_reasons],
                "event_evidence": _event_context(current.trading_code, events, as_of),
            }
        )
    records.sort(key=lambda item: str(item["trading_code"]))
    classification_counts = Counter(
        record["instrument"]["verification_status"] for record in records
    )
    data_counts = Counter(record["market_observation"]["data_status"] for record in records)
    returns = [
        value
        for record in records
        if (value := _feature_value_from_payload(record, "daily_return_pct")) is not None
    ]
    volume_anomalies = [
        value
        for record in records
        if (value := _feature_value_from_payload(record, "volume_multiple")) is not None
    ]
    verified_records = [
        record
        for record in records
        if record["instrument"]["verification_status"]
        == VerificationStatus.VERIFIED_EQUITY.value
    ]
    verified_returns = [
        value
        for record in verified_records
        if (value := _feature_value_from_payload(record, "daily_return_pct")) is not None
    ]
    feature_status_counts: dict[str, dict[str, int]] = {}
    for feature_name in records[0]["features"] if records else ():
        counts = Counter(record["features"][feature_name]["status"] for record in records)
        feature_status_counts[feature_name] = {
            status.value: counts[status.value] for status in FeatureStatus
        }
    report = {
        "schema": WATCHTOWER_SCHEMA,
        "title": f"DSE WATCHTOWER — {latest.market_date.isoformat()}",
        "market_date": latest.market_date.isoformat(),
        "as_of": as_of.isoformat(),
        "purpose": "Which DSE securities deserve human investigation today, and why?",
        "recommendations_generated": False,
        "source": {
            "latest_day_end_file": str(latest.source_path),
            "latest_day_end_sha256": latest.source_sha256,
            "sessions_loaded": len(ordered_sessions),
            "first_session": ordered_sessions[0].market_date.isoformat(),
            "latest_session": latest.market_date.isoformat(),
            "all_day_end_sources": [
                {
                    "market_date": session.market_date.isoformat(),
                    "path": str(session.source_path),
                    "sha256": session.source_sha256,
                }
                for session in ordered_sessions
            ],
        },
        "broad_market_summary": {
            "securities_observed": len(records),
            "verified_equities_observed": classification_counts[
                VerificationStatus.VERIFIED_EQUITY.value
            ],
            "unverified_instruments_observed": classification_counts[
                VerificationStatus.UNVERIFIED_INSTRUMENT.value
            ],
            "non_equities_observed": classification_counts[VerificationStatus.NON_EQUITY.value],
            "traded_usable": data_counts[DataStatus.USABLE.value],
            "zero_activity": data_counts[DataStatus.ZERO_ACTIVITY.value],
            "data_issue_rows": data_counts[DataStatus.DATA_ISSUE.value],
            "advancing": sum(value > 0 for value in returns),
            "declining": sum(value < 0 for value in returns),
            "unchanged": sum(value == 0 for value in returns),
            "median_return_pct": _decimal_text(decimal_median(returns)) if returns else None,
            "median_volume_anomaly": (
                _decimal_text(decimal_median(volume_anomalies)) if volume_anomalies else None
            ),
            "verified_equity_advancing": sum(value > 0 for value in verified_returns),
            "verified_equity_declining": sum(value < 0 for value in verified_returns),
            "verified_equity_unchanged": sum(value == 0 for value in verified_returns),
            "sector_breadth": _sector_breadth(records),
        },
        "feature_policy": {
            "trailing_window": TRAILING_WINDOW,
            "minimum_valid_prior_sessions": MINIMUM_HISTORY,
            "baseline": "median_and_mad",
            "profit_optimized_thresholds": False,
            "status_counts": feature_status_counts,
        },
        "attention_score_policy": {
            "deterministic": True,
            "event_evidence_changes_score": False,
            "tier_e_changes_score_or_label": False,
            "watch_threshold": 4,
            "high_attention_threshold": 8,
            "unverified_instruments_can_be_watchlist_candidates": False,
        },
        "manual_evidence_needed": {
            "instrument_master": [
                "Manually save https://www.dsebd.org/company_listing.php",
                "Manually save https://www.dsebd.org/by_industrylisting.php",
                "Manually save official DSE company profile pages needed to verify instrument type, category and listing status",
            ],
            "events": [
                "Manually save relevant DSE company/news disclosures",
                "Manually save relevant BSEC orders or issuer-filed releases",
                "Record publication_time, observed_at, source tier and source reference without inferring causality",
            ],
        },
        "safety": {
            "TRADING_MODE": "paper",
            "LIVE_TRADING_ENABLED": False,
            "BROKER_ADAPTER": "disabled",
            "database_used": False,
            "orders_created": 0,
            "fills_created": 0,
            "transactions_created": 0,
            "network_used": False,
            "recommendations_generated": False,
        },
        "isolation_proof": {
            "protected_database_sha256_before": protected_database_sha256,
            "protected_database_sha256_after_scan": protected_database_sha256,
            "forward_evidence_modified": False,
        },
        "records": records,
    }
    return report


def _feature_value_from_payload(record: Mapping[str, Any], name: str) -> Decimal | None:
    feature = record["features"][name]
    if feature["status"] != FeatureStatus.AVAILABLE.value or feature["value"] is None:
        return None
    try:
        return Decimal(str(feature["value"]))
    except InvalidOperation:
        return None


def _ranked(records: Sequence[dict[str, Any]], labels: set[str]) -> list[dict[str, Any]]:
    return sorted(
        (record for record in records if record["report_label"] in labels),
        key=lambda item: (-int(item["attention_score"]["total"]), str(item["trading_code"])),
    )


def render_watchtower_csv(report: Mapping[str, Any]) -> bytes:
    columns = (
        "market_date",
        "trading_code",
        "company_name",
        "sector",
        "verification_status",
        "instrument_type",
        "market_category",
        "listing_status",
        "data_status",
        "open",
        "high",
        "low",
        "close",
        "ltp",
        "ycp",
        "volume",
        "trade_count",
        "traded_value_mn",
        "daily_return_pct",
        "opening_gap_pct",
        "intraday_range_pct",
        "volume_multiple",
        "trade_count_multiple",
        "traded_value_multiple",
        "robust_return_z",
        "robust_volume_z",
        "volatility_expansion",
        "recent_high_low_breakout",
        "attention_score",
        "attention_components",
        "report_label",
        "watchlist_candidate_eligible",
        "event_evidence_status",
        "causality_inferred",
        "contradictory_evidence_present",
        "why_flagged",
    )
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=columns, lineterminator="\n")
    writer.writeheader()
    for record in report["records"]:
        features = record["features"]
        instrument = record["instrument"]
        market = record["market_observation"]
        writer.writerow(
            {
                "market_date": market["market_date"],
                "trading_code": record["trading_code"],
                "company_name": instrument["company_name"],
                "sector": instrument["sector"],
                "verification_status": instrument["verification_status"],
                "instrument_type": instrument["instrument_type"],
                "market_category": instrument["market_category"],
                "listing_status": instrument["listing_status"],
                "data_status": market["data_status"],
                "open": market["open"],
                "high": market["high"],
                "low": market["low"],
                "close": market["close"],
                "ltp": market["ltp"],
                "ycp": market["ycp"],
                "volume": market["volume"],
                "trade_count": market["trade_count"],
                "traded_value_mn": market["traded_value_mn"],
                **{name: features[name]["value"] for name in features},
                "attention_score": record["attention_score"]["total"],
                "attention_components": json.dumps(
                    record["attention_score"]["components"], sort_keys=True, separators=(",", ":")
                ),
                "report_label": record["report_label"],
                "watchlist_candidate_eligible": str(
                    record["watchlist_candidate_eligible"]
                ).lower(),
                "event_evidence_status": record["event_evidence"]["status"],
                "causality_inferred": "false",
                "contradictory_evidence_present": str(
                    record["event_evidence"]["contradictory_evidence_present"]
                ).lower(),
                "why_flagged": "; ".join(record["why_flagged"]),
            }
        )
    return stream.getvalue().encode("utf-8")


def _feature_display(record: Mapping[str, Any], name: str) -> str:
    feature = record["features"][name]
    if feature["value"] is None:
        return f"{feature['status']} ({feature['reason']})"
    return f"{feature['value']} {feature['unit']}"


def render_watchtower_markdown(report: Mapping[str, Any]) -> bytes:
    summary = report["broad_market_summary"]
    records = report["records"]
    lines = [
        f"# {report['title']}",
        "",
        "Investigation scanner only. The system may abstain completely.",
        "",
        "## Broad market",
        "",
        f"- Securities observed: {summary['securities_observed']}",
        f"- Verified equities: {summary['verified_equities_observed']}",
        f"- Unverified instruments: {summary['unverified_instruments_observed']}",
        f"- Non-equities: {summary['non_equities_observed']}",
        f"- Traded/usable: {summary['traded_usable']}",
        f"- Zero activity: {summary['zero_activity']}",
        f"- Advancing / declining / unchanged: {summary['advancing']} / {summary['declining']} / {summary['unchanged']}",
        f"- Median return: {summary['median_return_pct'] if summary['median_return_pct'] is not None else 'unavailable'}%",
        f"- Median volume anomaly: {summary['median_volume_anomaly'] if summary['median_volume_anomaly'] is not None else 'unavailable'}",
        "",
    ]

    def add_ranked_section(title: str, labels: set[str]) -> None:
        lines.extend([f"## {title}", ""])
        ranked = _ranked(records, labels)
        if not ranked:
            lines.extend(["None — Watchtower abstained.", ""])
            return
        for index, record in enumerate(ranked[:20], 1):
            lines.extend(
                [
                    f"{index}. **{record['trading_code']}** — ATTENTION SCORE {record['attention_score']['total']}",
                    f"   - Return anomaly: {_feature_display(record, 'daily_return_pct')}",
                    f"   - Volume anomaly: {_feature_display(record, 'volume_multiple')}",
                    f"   - Trade-count anomaly: {_feature_display(record, 'trade_count_multiple')}",
                    f"   - Event evidence: {record['event_evidence']['status']} (causality not inferred)",
                    f"   - Data quality: {record['market_observation']['data_status']}",
                    f"   - Why flagged: {'; '.join(record['why_flagged'])}",
                ]
            )
        lines.append("")

    add_ranked_section("HIGH ATTENTION", {"HIGH_ATTENTION"})
    add_ranked_section("WATCH", {"WATCH"})
    lines.extend(["## INSUFFICIENT HISTORY", ""])
    insufficient = _ranked(records, {"INSUFFICIENT_HISTORY"})
    if insufficient:
        lines.append(
            f"{len(insufficient)} verified equities lack the required {MINIMUM_HISTORY} valid prior sessions."
        )
    else:
        lines.append("No verified-equity candidate reached this state.")
    lines.extend(["", "## DATA ISSUES", ""])
    issues = _ranked(records, {"DATA_ISSUE"})
    lines.append(f"{len(issues)} observations are blocked from watchlist candidacy.")
    if issues:
        lines.extend(
            [
                "",
                "Highest raw anomaly observations among blocked records (not watchlist candidates):",
                "",
            ]
        )
        for index, record in enumerate(issues[:10], 1):
            lines.append(
                f"{index}. {record['trading_code']} — score {record['attention_score']['total']}; "
                f"{'; '.join(record['why_flagged'])}; {record['event_evidence']['status']}"
            )
    lines.extend(["", "## Feature availability", ""])
    for name, counts in report["feature_policy"]["status_counts"].items():
        lines.append(
            f"- {name}: available {counts['AVAILABLE']}; insufficient history "
            f"{counts['INSUFFICIENT_HISTORY']}; unavailable {counts['UNAVAILABLE']}"
        )
    lines.extend(["", "## Verified-sector breadth", ""])
    sectors = summary["sector_breadth"]
    if sectors:
        for sector in sectors:
            lines.append(
                f"- {sector['sector']}: {sector['breadth_label']}; advancing share "
                f"{sector['advancing_share']}; median return {sector['median_return_pct']}%"
            )
    else:
        lines.append("Unavailable — no sufficiently populated verified sector assignments.")
    lines.extend(
        [
            "",
            "## Operator evidence needed",
            "",
            "- Save the official DSE company listing and industry listing pages locally.",
            "- Save the relevant official company profile pages to verify type, category and status.",
            "- Add manual event records with publication and observation timestamps when available.",
            "",
            "**NO BUY/SELL RECOMMENDATIONS.**",
            "",
        ]
    )
    return "\n".join(lines).encode("utf-8")


def _canonical_json_bytes(report: Mapping[str, Any]) -> bytes:
    return (json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")


def _write_same_or_new(path: Path, payload: bytes) -> None:
    if path.exists():
        if path.read_bytes() != payload:
            raise WatchtowerError(f"Refusing to overwrite a different Watchtower artifact: {path}")
        return
    path.write_bytes(payload)


def run_watchtower(
    *,
    day_end_directory: Path,
    instrument_master_path: Path | None,
    event_evidence_path: Path | None,
    output_root: Path,
    protected_database_path: Path | None = None,
) -> dict[str, Any]:
    database_hash_before = (
        _sha256_file(protected_database_path)
        if protected_database_path is not None and protected_database_path.is_file()
        else None
    )
    sessions = load_day_end_sessions(day_end_directory)
    source_hashes_before = {session.source_path: session.source_sha256 for session in sessions}
    master = load_instrument_master(instrument_master_path)
    events = load_event_evidence(event_evidence_path)
    report = build_watchtower_report(
        sessions,
        master,
        events,
        protected_database_sha256=database_hash_before,
    )
    database_hash_after_scan = (
        _sha256_file(protected_database_path)
        if protected_database_path is not None and protected_database_path.is_file()
        else None
    )
    if database_hash_after_scan != database_hash_before:
        raise WatchtowerError("Protected operational database changed during Watchtower scan")
    for path, expected_hash in source_hashes_before.items():
        if _sha256_file(path) != expected_hash:
            raise WatchtowerError(f"Operator-owned Day End source changed during scan: {path}")
    report["isolation_proof"]["protected_database_sha256_after_scan"] = database_hash_after_scan
    market_date = str(report["market_date"])
    output_directory = output_root / market_date
    output_directory.mkdir(parents=True, exist_ok=True)
    paths = {
        "json": output_directory / f"watchtower_{market_date}.json",
        "csv": output_directory / f"watchtower_{market_date}.csv",
        "markdown": output_directory / f"watchtower_{market_date}.md",
    }
    _write_same_or_new(paths["json"], _canonical_json_bytes(report))
    _write_same_or_new(paths["csv"], render_watchtower_csv(report))
    _write_same_or_new(paths["markdown"], render_watchtower_markdown(report))
    final_database_hash = (
        _sha256_file(protected_database_path)
        if protected_database_path is not None and protected_database_path.is_file()
        else None
    )
    if final_database_hash != database_hash_before:
        raise WatchtowerError("Protected operational database changed while writing reports")
    return {
        "market_date": market_date,
        "summary": report["broad_market_summary"],
        "artifacts": {name: str(path.resolve()) for name, path in paths.items()},
        "protected_database_sha256_before": database_hash_before,
        "protected_database_sha256_after": final_database_hash,
    }
