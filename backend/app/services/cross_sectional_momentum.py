from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from statistics import mean, pstdev
from typing import Any, cast
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy.orm import Session

from app.models import ResearchDataset
from app.schemas.market import HistoricalBar, TimestampProvenance
from app.services.historical_strategy_research import (
    BASELINE_FEE_PERCENT,
    BASELINE_SLIPPAGE_PERCENT,
    STARTING_CAPITAL,
    _curve_metrics,
    sha256_file,
)

STRATEGY_ID = "cross_sectional_momentum"
STRATEGY_VERSION = "0.1.0"
STRATEGY_IDENTITY = f"{STRATEGY_ID}@{STRATEGY_VERSION}"
UNIVERSE = (
    "GP",
    "ACI",
    "BRACBANK",
    "BATBC",
    "SQURPHARMA",
    "IDLC",
    "LANKABAFIN",
    "POWERGRID",
    "RENATA",
    "BERGERPBL",
)
PARENT_SYMBOLS = ("GP", "ACI", "BRACBANK")
EXTENSION_SYMBOLS = tuple(symbol for symbol in UNIVERSE if symbol not in PARENT_SYMBOLS)
MAX_TARGET_WEIGHT = 0.35
TRADING_DAYS = 252
APPROVED_DISPOSITIONS = {
    "tier_1_cross_source_confirmed",
    "tier_2_single_source_high_quality",
}
EXPECTED_DATASETS: tuple[dict[str, Any], ...] = (
    {
        "id": "ba5f2d99-6c66-4e37-ae31-d48c8ee47b15",
        "version": "gp-aci-bracbank-research-f24a48cb729e8a65",
        "sha256": "ddfeee6bbf5324f9f64fd604e9d4bfc7bd2a45ce2896e7b968037af58de04791",
        "symbols": list(PARENT_SYMBOLS),
        "origin": "parent",
    },
    {
        "id": "c6da44f7-b842-4d0a-a8e0-31bad7f96bea",
        "version": "batbc-squrpharma-t2-extension-5357a454f66e1ea7",
        "sha256": "4470633c8d62c627357e6c0a6472466142e7a6539f7100d9de677827dda8c882",
        "symbols": ["BATBC", "SQURPHARMA"],
        "origin": "extension_1",
    },
    {
        "id": "8f43a2ed-7d78-4b57-8c30-1fb75db0e939",
        "version": "five-symbol-t2-extension-789c90798e1b4268",
        "sha256": "21b9dda627d7045fb42aadb397de6d09be0cf693d9d17931b11df72ace838f10",
        "symbols": ["IDLC", "LANKABAFIN", "POWERGRID", "RENATA", "BERGERPBL"],
        "origin": "extension_2",
    },
)
PRIMARY_PARAMETERS: dict[str, Any] = {
    "lookback_months": 12,
    "skip_recent_months": 1,
    "top_n": 3,
    "rebalance_frequency": "monthly",
    "weighting": "equal",
    "maximum_target_weight": MAX_TARGET_WEIGHT,
    "long_only": True,
    "leverage": False,
    "short_selling": False,
    "fee_percent": str(BASELINE_FEE_PERCENT),
    "slippage_percent": str(BASELINE_SLIPPAGE_PERCENT),
    "execution": "next_common_source_present_open",
}


@dataclass(frozen=True)
class MomentumConfig:
    name: str
    lookback_months: int
    top_n: int
    rebalance_frequency: str = "monthly"
    weighting: str = "equal"


PRIMARY_CONFIG = MomentumConfig("primary_12m_skip1_top3_monthly", 12, 3)
SENSITIVITY_CONFIGS = (
    MomentumConfig("variant_a_6m_skip1_top3_monthly", 6, 3),
    MomentumConfig("variant_b_12m_skip1_top5_monthly", 12, 5),
    MomentumConfig("variant_c_12m_skip1_top3_quarterly", 12, 3, "quarterly"),
    MomentumConfig(
        "variant_d_12m_skip1_top3_inverse_volatility",
        12,
        3,
        "monthly",
        "inverse_volatility",
    ),
)


@dataclass
class PortfolioRun:
    name: str
    metrics: dict[str, Any]
    equity_curve: list[dict[str, Any]]
    ledger: list[dict[str, Any]]
    rebalances: list[dict[str, Any]]
    symbol_contribution: dict[str, float]
    dataset_contribution: dict[str, float]
    monthly_returns: dict[str, float]
    yearly_returns: dict[str, float]
    time_held: dict[str, dict[str, int]]
    cash_periods: list[dict[str, Any]]


@dataclass
class ResearchBundle:
    payload: dict[str, Any]
    ledger: list[dict[str, Any]]


def canonical_hash(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def parameter_hash() -> str:
    return canonical_hash(PRIMARY_PARAMETERS)


def code_hash(repository_root: Path) -> str:
    implementation = repository_root / "backend" / "app" / "services" / Path(__file__).name
    payload = implementation.read_bytes() + b"\0" + STRATEGY_IDENTITY.encode()
    return hashlib.sha256(payload).hexdigest()


def deterministic_registration_id(
    *, code_sha256: str, parameter_sha256: str, datasets: Sequence[Mapping[str, Any]]
) -> str:
    identity = canonical_hash(
        {
            "strategy": STRATEGY_IDENTITY,
            "code_hash": code_sha256,
            "parameter_hash": parameter_sha256,
            "datasets": [
                {"id": row["id"], "sha256": row["sha256"]}
                for row in sorted(datasets, key=lambda value: str(value["id"]))
            ],
        }
    )
    return str(uuid5(NAMESPACE_URL, f"dse-autotrader:{identity}"))


def _dataset_path(dataset: ResearchDataset, repository_root: Path) -> Path:
    path = Path(dataset.normalized_file_path)
    return path if path.is_absolute() else repository_root / path


def _complete_lineage(row: Mapping[str, Any]) -> bool:
    hashes = row.get("raw_hashes") or row.get("raw_file_hashes")
    audit = row.get("audit_linkage") or row.get("audit_event_ids")
    return bool(
        row.get("selected_source")
        and row.get("source_lineage")
        and row.get("source_row_ids")
        and hashes
        and audit
    )


def load_active_universe(
    db: Session, repository_root: Path
) -> tuple[dict[str, list[HistoricalBar]], dict[str, Any], list[dict[str, Any]]]:
    bars: dict[str, list[HistoricalBar]] = {symbol: [] for symbol in UNIVERSE}
    identities: list[dict[str, Any]] = []
    full_keys: set[tuple[str, str, str]] = set()
    adjusted_keys: set[tuple[str, str]] = set()
    total_rows = invalid = incomplete = ineligible = 0
    origins: dict[str, str] = {}
    adjusted_counts: Counter[str] = Counter()

    for expected in EXPECTED_DATASETS:
        dataset = db.get(ResearchDataset, str(expected["id"]))
        if (
            dataset is None
            or dataset.name != expected["version"]
            or dataset.dataset_hash != expected["sha256"]
            or dataset.status != "research_dataset_active"
            or set(dataset.symbols) != set(expected["symbols"])
        ):
            raise ValueError(f"Active dataset identity mismatch: {expected['id']}")
        path = _dataset_path(dataset, repository_root)
        if not path.is_file() or sha256_file(path) != expected["sha256"]:
            raise ValueError(f"Active dataset file mismatch: {expected['id']}")
        identities.append(
            {
                "id": dataset.id,
                "version": dataset.name,
                "sha256": dataset.dataset_hash,
                "symbols": list(dataset.symbols),
                "origin": expected["origin"],
                "path": path.resolve().relative_to(repository_root.resolve()).as_posix(),
            }
        )
        for symbol in dataset.symbols:
            origins[symbol] = str(expected["origin"])
        observed_symbols: set[str] = set()
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                total_rows += 1
                row = cast(dict[str, Any], json.loads(line))
                symbol = str(row.get("symbol"))
                day = str(row.get("date"))
                grain = str(row.get("adjustment_status"))
                observed_symbols.add(symbol)
                key = (symbol, day, grain)
                if key in full_keys:
                    raise ValueError(f"Duplicate active symbol/date/grain: {key}")
                full_keys.add(key)
                disposition = str(row.get("final_disposition") or row.get("quality_tier") or "")
                ineligible += int(disposition not in APPROVED_DISPOSITIONS)
                incomplete += int(not _complete_lineage(row))
                try:
                    open_, high, low, close = (
                        float(row[field]) for field in ("open", "high", "low", "close")
                    )
                    if not (
                        math.isfinite(open_)
                        and math.isfinite(high)
                        and math.isfinite(low)
                        and math.isfinite(close)
                        and 0 <= low <= min(open_, close) <= max(open_, close) <= high
                    ):
                        invalid += 1
                except (KeyError, TypeError, ValueError):
                    invalid += 1
                    continue
                if grain not in {"adjusted", "unadjusted"}:
                    invalid += 1
                    continue
                if grain != "adjusted":
                    continue
                adjusted_key = (symbol, day)
                if adjusted_key in adjusted_keys:
                    raise ValueError(f"Duplicate adjusted symbol/date: {adjusted_key}")
                adjusted_keys.add(adjusted_key)
                adjusted_counts[symbol] += 1
                bars[symbol].append(
                    HistoricalBar(
                        timestamp=datetime.combine(
                            date.fromisoformat(day), datetime.min.time(), UTC
                        ),
                        symbol=symbol,
                        open=str(row["open"]),
                        high=str(row["high"]),
                        low=str(row["low"]),
                        close=str(row["close"]),
                        volume=int(float(row["volume"])) if row.get("volume") is not None else None,
                        source=str(row["selected_source"]),
                        timestamp_provenance=TimestampProvenance.UNKNOWN,
                        quality_flags=[disposition, str(expected["origin"])],
                    )
                )
        if observed_symbols != set(expected["symbols"]):
            raise ValueError(f"Dataset symbol inventory mismatch: {expected['id']}")

    for symbol in UNIVERSE:
        bars[symbol].sort(key=lambda item: item.timestamp)
    date_sets = [{item.timestamp.date() for item in bars[symbol]} for symbol in UNIVERSE]
    common_dates = sorted(set.intersection(*date_sets))
    checks = {
        "mandatory_passed": bool(
            set(origins) == set(UNIVERSE)
            and all(bars.values())
            and not invalid
            and not incomplete
            and not ineligible
            and len(common_dates) > 13 * 15
        ),
        "symbols": list(UNIVERSE),
        "dataset_count": len(identities),
        "total_active_rows": total_rows,
        "adjusted_rows_by_symbol": dict(adjusted_counts),
        "invalid_rows": invalid,
        "incomplete_lineage_rows": incomplete,
        "ineligible_rows": ineligible,
        "common_source_present_sessions": len(common_dates),
        "common_start": common_dates[0].isoformat() if common_dates else None,
        "common_end": common_dates[-1].isoformat() if common_dates else None,
        "common_calendar_rule": (
            "intersection of adjusted source-present symbol dates; no prices are forward-filled"
        ),
        "symbol_dataset_origin": origins,
    }
    if not checks["mandatory_passed"]:
        raise ValueError(f"Ten-symbol active-universe validation failed: {checks}")
    return bars, checks, identities


def _month_index(value: date) -> int:
    return value.year * 12 + value.month - 1


def _month_label(index: int) -> str:
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


def _aligned_data(
    bars: Mapping[str, Sequence[HistoricalBar]],
) -> tuple[list[date], dict[str, dict[date, HistoricalBar]], dict[int, date]]:
    by_symbol = {
        symbol: {item.timestamp.date(): item for item in values} for symbol, values in bars.items()
    }
    common = sorted(set.intersection(*(set(values) for values in by_symbol.values())))
    month_ends: dict[int, date] = {}
    for day in common:
        month_ends[_month_index(day)] = day
    return common, by_symbol, month_ends


def momentum_scores(
    bars: Mapping[str, Sequence[HistoricalBar]],
    signal_date: date,
    *,
    lookback_months: int,
) -> tuple[dict[str, float], dict[str, str]]:
    _, by_symbol, month_ends = _aligned_data(bars)
    signal_month = _month_index(signal_date)
    end_month = signal_month - 1
    start_month = end_month - lookback_months
    required = range(start_month, end_month + 1)
    reasons: dict[str, str] = {}
    scores: dict[str, float] = {}
    for symbol in sorted(bars):
        if signal_date not in by_symbol[symbol]:
            reasons[symbol] = "signal_session_missing"
            continue
        if any(month not in month_ends for month in required):
            reasons[symbol] = "common_month_missing"
            continue
        if any(month_ends[month] not in by_symbol[symbol] for month in required):
            reasons[symbol] = "required_symbol_month_missing"
            continue
        start = float(by_symbol[symbol][month_ends[start_month]].close)
        end = float(by_symbol[symbol][month_ends[end_month]].close)
        if start <= 0 or not math.isfinite(start) or not math.isfinite(end):
            reasons[symbol] = "invalid_adjusted_lookback"
            continue
        scores[symbol] = end / start - 1
    return scores, reasons


def _inverse_volatility_weights(
    selected: Sequence[str],
    *,
    signal_date: date,
    by_symbol: Mapping[str, Mapping[date, HistoricalBar]],
    common_dates: Sequence[date],
    top_n: int,
) -> dict[str, float]:
    prior = [day for day in common_dates if day < date(signal_date.year, signal_date.month, 1)]
    raw: dict[str, float] = {}
    for symbol in selected:
        closes = [float(by_symbol[symbol][day].close) for day in prior[-61:]]
        if len(closes) != 61:
            continue
        returns = [closes[index] / closes[index - 1] - 1 for index in range(1, len(closes))]
        volatility = pstdev(returns)
        if volatility > 0 and math.isfinite(volatility):
            raw[symbol] = 1 / volatility
    if set(raw) != set(selected):
        return {}
    target_total = len(selected) / top_n
    remaining = set(selected)
    weights: dict[str, float] = {}
    unallocated = target_total
    while remaining:
        scale = unallocated / sum(raw[symbol] for symbol in remaining)
        capped = {symbol for symbol in remaining if raw[symbol] * scale > MAX_TARGET_WEIGHT}
        if not capped:
            weights.update({symbol: raw[symbol] * scale for symbol in remaining})
            break
        for symbol in sorted(capped):
            weights[symbol] = MAX_TARGET_WEIGHT
            unallocated -= MAX_TARGET_WEIGHT
            remaining.remove(symbol)
    return weights


def build_rebalance_plans(
    bars: Mapping[str, Sequence[HistoricalBar]], config: MomentumConfig
) -> list[dict[str, Any]]:
    common_dates, by_symbol, month_ends = _aligned_data(bars)
    first_month = min(month_ends)
    next_date = {
        common_dates[index]: common_dates[index + 1] for index in range(len(common_dates) - 1)
    }
    plans: list[dict[str, Any]] = []
    for month, signal_date in sorted(month_ends.items()):
        if month < first_month + config.lookback_months + 1:
            continue
        if signal_date not in next_date:
            continue
        if config.rebalance_frequency == "quarterly" and signal_date.month not in {3, 6, 9, 12}:
            continue
        scores, exclusions = momentum_scores(
            bars, signal_date, lookback_months=config.lookback_months
        )
        ranked = sorted(scores, key=lambda symbol: (-scores[symbol], symbol))
        selected = ranked[: config.top_n]
        if config.weighting == "inverse_volatility":
            weights = _inverse_volatility_weights(
                selected,
                signal_date=signal_date,
                by_symbol=by_symbol,
                common_dates=common_dates,
                top_n=config.top_n,
            )
            if selected and not weights:
                exclusions.update(
                    {symbol: "inverse_volatility_history_missing" for symbol in selected}
                )
                selected = []
        else:
            weights = {symbol: 1 / config.top_n for symbol in selected}
        if any(weight > MAX_TARGET_WEIGHT + 1e-12 for weight in weights.values()):
            raise ValueError("Target weight cap exceeded")
        plans.append(
            {
                "month": _month_label(month),
                "signal_date": signal_date,
                "execution_date": next_date[signal_date],
                "scores": scores,
                "ranking": ranked,
                "selected": selected,
                "target_weights": weights,
                "eligibility_exclusions": exclusions,
            }
        )
    return plans


def _period_returns(curve: Sequence[Mapping[str, Any]], period: str) -> dict[str, float]:
    ending: dict[str, float] = {}
    for point in curve:
        day = date.fromisoformat(str(point["timestamp"])[:10])
        label = f"{day.year:04d}-{day.month:02d}" if period == "month" else str(day.year)
        ending[label] = float(point["equity"])
    output: dict[str, float] = {}
    prior = float(STARTING_CAPITAL)
    for label, value in ending.items():
        output[label] = (value / prior - 1) * 100
        prior = value
    return output


def _cash_periods(curve: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    periods: list[dict[str, Any]] = []
    start: str | None = None
    end: str | None = None
    sessions = 0
    for point in curve:
        day = str(point["timestamp"])[:10]
        if float(point["invested_exposure_percent"]) <= 1e-12:
            start = start or day
            end = day
            sessions += 1
        elif start is not None:
            periods.append({"start": start, "end": end, "sessions": sessions})
            start = end = None
            sessions = 0
    if start is not None:
        periods.append({"start": start, "end": end, "sessions": sessions})
    return periods


def simulate_plans(
    bars: Mapping[str, Sequence[HistoricalBar]],
    plans: Sequence[Mapping[str, Any]],
    *,
    name: str,
    fee_percent: float = float(BASELINE_FEE_PERCENT),
    slippage_percent: float = float(BASELINE_SLIPPAGE_PERCENT),
) -> PortfolioRun:
    if not plans:
        raise ValueError("No eligible rebalance plan")
    common_dates, by_symbol, _ = _aligned_data(bars)
    start_date = cast(date, plans[0]["signal_date"])
    dates = [day for day in common_dates if day >= start_date]
    plan_by_execution = {cast(date, row["execution_date"]): row for row in plans}
    cash = float(STARTING_CAPITAL)
    holdings = {symbol: 0 for symbol in bars}
    fees = slippage = traded_notional = 0.0
    ledger: list[dict[str, Any]] = []
    curve: list[dict[str, Any]] = []
    held_sessions = Counter[str]()
    first_held: dict[str, date] = {}
    last_held: dict[str, date] = {}
    fee_rate = fee_percent / 100
    slip_rate = slippage_percent / 100

    for day in dates:
        if day in plan_by_execution:
            plan = plan_by_execution[day]
            opening_equity = cash + sum(
                holdings[symbol] * float(by_symbol[symbol][day].open) for symbol in bars
            )
            target_quantities: dict[str, int] = {}
            for symbol in bars:
                weight = float(cast(Mapping[str, float], plan["target_weights"]).get(symbol, 0.0))
                conservative_buy = float(by_symbol[symbol][day].open) * (1 + slip_rate)
                target_quantities[symbol] = int(
                    opening_equity * weight / (conservative_buy * (1 + fee_rate))
                )
            ledger.append(
                {
                    "experiment": name,
                    "event_type": "rebalance",
                    "signal_timestamp": cast(date, plan["signal_date"]).isoformat(),
                    "execution_timestamp": day.isoformat(),
                    "symbol": "",
                    "side": "",
                    "quantity": 0,
                    "source_open": None,
                    "fill_price": None,
                    "fee": 0.0,
                    "slippage": 0.0,
                    "cash_after": cash,
                    "selected": list(plan["selected"]),
                    "target_weights": dict(cast(Mapping[str, float], plan["target_weights"])),
                    "ranking": list(plan["ranking"]),
                }
            )
            for symbol in sorted(bars):
                quantity = holdings[symbol] - target_quantities[symbol]
                if quantity <= 0:
                    continue
                source_open = float(by_symbol[symbol][day].open)
                fill = source_open * (1 - slip_rate)
                fee = quantity * fill * fee_rate
                cash += quantity * fill - fee
                holdings[symbol] -= quantity
                fees += fee
                slippage_value = quantity * (source_open - fill)
                slippage += slippage_value
                traded_notional += quantity * fill
                ledger.append(
                    {
                        "experiment": name,
                        "event_type": "trade",
                        "signal_timestamp": cast(date, plan["signal_date"]).isoformat(),
                        "execution_timestamp": day.isoformat(),
                        "symbol": symbol,
                        "side": "sell",
                        "quantity": quantity,
                        "source_open": source_open,
                        "fill_price": fill,
                        "fee": fee,
                        "slippage": slippage_value,
                        "cash_after": cash,
                        "selected": list(plan["selected"]),
                        "target_weights": dict(cast(Mapping[str, float], plan["target_weights"])),
                        "ranking": list(plan["ranking"]),
                    }
                )
            buy_order = list(plan["selected"])
            for symbol in buy_order:
                desired = target_quantities[symbol] - holdings[symbol]
                if desired <= 0:
                    continue
                source_open = float(by_symbol[symbol][day].open)
                fill = source_open * (1 + slip_rate)
                unit_cost = fill * (1 + fee_rate)
                quantity = min(desired, int(cash / unit_cost))
                if quantity <= 0:
                    continue
                fee = quantity * fill * fee_rate
                cash -= quantity * fill + fee
                if cash < -1e-7:
                    raise RuntimeError("Portfolio cash became negative")
                holdings[symbol] += quantity
                fees += fee
                slippage_value = quantity * (fill - source_open)
                slippage += slippage_value
                traded_notional += quantity * fill
                ledger.append(
                    {
                        "experiment": name,
                        "event_type": "trade",
                        "signal_timestamp": cast(date, plan["signal_date"]).isoformat(),
                        "execution_timestamp": day.isoformat(),
                        "symbol": symbol,
                        "side": "buy",
                        "quantity": quantity,
                        "source_open": source_open,
                        "fill_price": fill,
                        "fee": fee,
                        "slippage": slippage_value,
                        "cash_after": cash,
                        "selected": list(plan["selected"]),
                        "target_weights": dict(cast(Mapping[str, float], plan["target_weights"])),
                        "ranking": list(plan["ranking"]),
                    }
                )

        market_values = {
            symbol: holdings[symbol] * float(by_symbol[symbol][day].close) for symbol in bars
        }
        equity = cash + sum(market_values.values())
        invested = sum(market_values.values())
        weights = {
            symbol: value / equity for symbol, value in market_values.items() if holdings[symbol]
        }
        for symbol, quantity in holdings.items():
            if quantity:
                held_sessions[symbol] += 1
                first_held.setdefault(symbol, day)
                last_held[symbol] = day
        curve.append(
            {
                "timestamp": day.isoformat(),
                "equity": equity,
                "cash": cash,
                "cash_exposure_percent": cash / equity * 100,
                "invested_exposure_percent": invested / equity * 100,
                "weights": weights,
            }
        )

    metrics = _curve_metrics(cast(list[dict[str, object]], curve), float(STARTING_CAPITAL))
    average_equity = mean(float(point["equity"]) for point in curve)
    average_exposure = mean(float(point["invested_exposure_percent"]) for point in curve)
    max_weight = max(
        (max(cast(dict[str, float], point["weights"]).values(), default=0.0) for point in curve),
        default=0.0,
    )
    average_hhi = mean(
        sum(value * value for value in cast(dict[str, float], point["weights"]).values())
        for point in curve
    )
    max_target = max(
        (
            max(cast(Mapping[str, float], plan["target_weights"]).values(), default=0.0)
            for plan in plans
        ),
        default=0.0,
    )
    symbol_contribution: dict[str, float] = {}
    for symbol in bars:
        buys = sum(
            float(row["fill_price"]) * int(row["quantity"]) + float(row["fee"])
            for row in ledger
            if row["event_type"] == "trade" and row["symbol"] == symbol and row["side"] == "buy"
        )
        sells = sum(
            float(row["fill_price"]) * int(row["quantity"]) - float(row["fee"])
            for row in ledger
            if row["event_type"] == "trade" and row["symbol"] == symbol and row["side"] == "sell"
        )
        final_value = holdings[symbol] * float(by_symbol[symbol][dates[-1]].close)
        symbol_contribution[symbol] = sells + final_value - buys
    origin_by_symbol = {
        symbol: ("parent" if symbol in PARENT_SYMBOLS else "extension") for symbol in bars
    }
    dataset_contribution = {
        origin: sum(
            symbol_contribution[symbol] for symbol in bars if origin_by_symbol[symbol] == origin
        )
        for origin in ("parent", "extension")
    }
    metrics.update(
        {
            "turnover": traded_notional / average_equity,
            "total_fees_bdt": fees,
            "total_slippage_bdt": slippage,
            "average_invested_exposure_percent": average_exposure,
            "average_cash_exposure_percent": 100 - average_exposure,
            "rebalance_count": len(plans),
            "trade_count": sum(row["event_type"] == "trade" for row in ledger),
            "maximum_target_weight_percent": max_target * 100,
            "maximum_realized_position_weight_percent": max_weight * 100,
            "average_concentration_hhi": average_hhi,
            "minimum_cash_bdt": min(float(point["cash"]) for point in curve),
        }
    )
    time_held = {
        symbol: {
            "source_present_sessions": held_sessions[symbol],
            "calendar_days_between_first_and_last_holding": (
                (last_held[symbol] - first_held[symbol]).days + 1 if symbol in first_held else 0
            ),
        }
        for symbol in bars
    }
    return PortfolioRun(
        name=name,
        metrics=metrics,
        equity_curve=curve,
        ledger=ledger,
        rebalances=[
            {
                **{
                    key: value
                    for key, value in row.items()
                    if key not in {"signal_date", "execution_date"}
                },
                "signal_date": cast(date, row["signal_date"]).isoformat(),
                "execution_date": cast(date, row["execution_date"]).isoformat(),
            }
            for row in plans
        ],
        symbol_contribution=symbol_contribution,
        dataset_contribution=dataset_contribution,
        monthly_returns=_period_returns(curve, "month"),
        yearly_returns=_period_returns(curve, "year"),
        time_held=time_held,
        cash_periods=_cash_periods(curve),
    )


def run_momentum(
    bars: Mapping[str, Sequence[HistoricalBar]],
    config: MomentumConfig,
    *,
    fee_percent: float = float(BASELINE_FEE_PERCENT),
    slippage_percent: float = float(BASELINE_SLIPPAGE_PERCENT),
) -> PortfolioRun:
    return simulate_plans(
        bars,
        build_rebalance_plans(bars, config),
        name=config.name,
        fee_percent=fee_percent,
        slippage_percent=slippage_percent,
    )


def _portfolio_summary(run: PortfolioRun) -> dict[str, Any]:
    """Return the complete, deterministic analysis contract without bulky daily rows."""
    monthly = run.monthly_returns
    return {
        "name": run.name,
        "metrics": run.metrics,
        "monthly_returns_percent": monthly,
        "yearly_returns_percent": run.yearly_returns,
        "winning_months": sum(value > 0 for value in monthly.values()),
        "losing_months": sum(value < 0 for value in monthly.values()),
        "flat_months": sum(value == 0 for value in monthly.values()),
        "symbol_contribution_bdt": run.symbol_contribution,
        "dataset_contribution_bdt": run.dataset_contribution,
        "time_held": run.time_held,
        "cash_periods": run.cash_periods,
        "rebalance_count": len(run.rebalances),
        "equity_curve_sha256": canonical_hash(run.equity_curve),
    }


def _static_weight_plans(
    bars: Mapping[str, Sequence[HistoricalBar]],
    *,
    name: str,
    weights: Mapping[str, float],
    start_signal: date,
    monthly: bool,
) -> list[dict[str, Any]]:
    common_dates, _, month_ends = _aligned_data(bars)
    next_date = {
        common_dates[index]: common_dates[index + 1] for index in range(len(common_dates) - 1)
    }
    signals = [
        signal
        for _, signal in sorted(month_ends.items())
        if signal >= start_signal and signal in next_date
    ]
    if not monthly:
        signals = signals[:1]
    return [
        {
            "month": f"{signal.year:04d}-{signal.month:02d}",
            "signal_date": signal,
            "execution_date": next_date[signal],
            "scores": {},
            "ranking": list(weights),
            "selected": list(weights),
            "target_weights": dict(weights),
            "eligibility_exclusions": {},
            "benchmark": name,
        }
        for signal in signals
    ]


def run_benchmarks(
    bars: Mapping[str, Sequence[HistoricalBar]], primary: PortfolioRun
) -> dict[str, PortfolioRun]:
    if not primary.rebalances:
        raise ValueError("Primary strategy has no rebalance observations")
    start_signal = date.fromisoformat(str(primary.rebalances[0]["signal_date"]))
    equal = {symbol: 1 / len(bars) for symbol in bars}
    half = {symbol: 0.5 / len(bars) for symbol in bars}
    configurations = {
        "equal_weight_buy_and_hold": (equal, False),
        "monthly_rebalanced_equal_weight": (equal, True),
        "half_equal_weight_equities_half_cash": (half, True),
    }
    return {
        name: simulate_plans(
            bars,
            _static_weight_plans(
                bars,
                name=name,
                weights=weights,
                start_signal=start_signal,
                monthly=monthly,
            ),
            name=name,
        )
        for name, (weights, monthly) in configurations.items()
    }


def _curve_window(curve: Sequence[Mapping[str, Any]], *, start: int, end: int) -> dict[str, Any]:
    selected = list(curve[start:end])
    if len(selected) < 2:
        raise ValueError("Evaluation window requires at least two observations")
    initial = float(selected[0]["equity"])
    metrics = _curve_metrics(cast(list[dict[str, object]], selected), initial)
    return {
        "start": str(selected[0]["timestamp"])[:10],
        "end": str(selected[-1]["timestamp"])[:10],
        "observations": len(selected),
        "metrics": metrics,
    }


def walk_forward_analysis(run: PortfolioRun) -> dict[str, Any]:
    """Fixed-parameter expanding-window partitions; holdouts are never optimized."""
    count = len(run.equity_curve)
    boundaries = [int(count * fraction) for fraction in (0.4, 0.6, 0.8, 1.0)]
    partitions: list[dict[str, Any]] = []
    holdout_start = boundaries[0]
    for index, holdout_end in enumerate(boundaries[1:], start=1):
        holdout = _curve_window(run.equity_curve, start=holdout_start, end=holdout_end)
        partitions.append(
            {
                "partition": index,
                "training_start": str(run.equity_curve[0]["timestamp"])[:10],
                "training_end": str(run.equity_curve[holdout_start - 1]["timestamp"])[:10],
                "parameters_selected_on_training": False,
                "holdout": holdout,
            }
        )
        holdout_start = holdout_end
    combined = _curve_window(run.equity_curve, start=boundaries[0], end=count)
    positive = sum(
        float(item["holdout"]["metrics"]["total_return_percent"]) > 0 for item in partitions
    )
    return {
        "method": "chronological_expanding_window_fixed_parameters",
        "random_splits": False,
        "holdout_optimization": False,
        "partitions": partitions,
        "combined_holdout": combined,
        "positive_holdouts": positive,
        "holdout_count": len(partitions),
    }


def subperiod_analysis(run: PortfolioRun) -> list[dict[str, Any]]:
    count = len(run.equity_curve)
    boundaries = [0, count // 3, (count * 2) // 3, count]
    return [
        {
            "subperiod": index + 1,
            **_curve_window(
                run.equity_curve,
                start=boundaries[index],
                end=boundaries[index + 1],
            ),
        }
        for index in range(3)
    ]


def _return(run: PortfolioRun) -> float:
    return float(run.metrics["total_return_percent"])


def _dependence_analysis(
    primary: PortfolioRun,
    leave_one_out: Mapping[str, PortfolioRun],
    isolated: Mapping[str, PortfolioRun],
    variants: Mapping[str, PortfolioRun],
    walk_forward: Mapping[str, Any],
) -> dict[str, Any]:
    pnl = float(primary.metrics["final_equity"]) - float(STARTING_CAPITAL)
    absolute_contributions = {
        symbol: abs(value) for symbol, value in primary.symbol_contribution.items()
    }
    total_absolute = sum(absolute_contributions.values())
    leading_symbol = max(absolute_contributions, key=lambda symbol: absolute_contributions[symbol])
    leading_share = (
        absolute_contributions[leading_symbol] / total_absolute if total_absolute else 0.0
    )
    origin_absolute = {origin: abs(value) for origin, value in primary.dataset_contribution.items()}
    origin_total = sum(origin_absolute.values())
    leading_origin = max(origin_absolute, key=lambda origin: origin_absolute[origin])
    return {
        "bracbank": {
            "primary_return_percent": _return(primary),
            "leave_out_return_percent": _return(leave_one_out["BRACBANK"]),
            "return_difference_percentage_points": _return(primary)
            - _return(leave_one_out["BRACBANK"]),
        },
        "one_symbol": {
            "largest_absolute_contributor": leading_symbol,
            "largest_absolute_contribution_share": leading_share,
            "strategy_pnl_bdt": pnl,
        },
        "one_period": {
            "positive_holdouts": walk_forward["positive_holdouts"],
            "holdout_count": walk_forward["holdout_count"],
            "subperiod_returns_percent": [
                float(item["metrics"]["total_return_percent"])
                for item in subperiod_analysis(primary)
            ],
        },
        "one_dataset": {
            "largest_absolute_dataset_contributor": leading_origin,
            "largest_absolute_dataset_contribution_share": (
                origin_absolute[leading_origin] / origin_total if origin_total else 0.0
            ),
            "parent_only_return_percent": _return(isolated["parent_only"]),
            "extensions_only_return_percent": _return(isolated["extensions_only"]),
        },
        "one_parameter_variant": {
            "returns_percent": {name: _return(run) for name, run in variants.items()},
            "positive_variant_count": sum(_return(run) > 0 for run in variants.values()),
        },
    }


def research_verdict(
    primary: PortfolioRun,
    dependence: Mapping[str, Any],
    walk_forward: Mapping[str, Any],
    variants: Mapping[str, PortfolioRun],
) -> str:
    primary_return = _return(primary)
    if primary_return <= 0 or float(primary.metrics["maximum_drawdown_percent"]) <= -50:
        return "reject_strategy"
    positive_holdouts = int(walk_forward["positive_holdouts"])
    positive_variants = sum(_return(run) > 0 for run in variants.values())
    leading_share = float(dependence["one_symbol"]["largest_absolute_contribution_share"])
    if positive_holdouts >= 2 and positive_variants >= 3 and leading_share <= 0.5:
        return "promising_but_unproven"
    return "insufficient_evidence"


def build_research_bundle(
    bars: Mapping[str, Sequence[HistoricalBar]],
    *,
    dataset_identities: Sequence[Mapping[str, Any]],
    validation: Mapping[str, Any],
    registration_identity: Mapping[str, Any],
    archived_ma_context: Mapping[str, Any],
) -> ResearchBundle:
    primary = run_momentum(bars, PRIMARY_CONFIG)
    gross = run_momentum(bars, PRIMARY_CONFIG, fee_percent=0.0, slippage_percent=0.0)
    sensitivities = {config.name: run_momentum(bars, config) for config in SENSITIVITY_CONFIGS}
    benchmarks = run_benchmarks(bars, primary)
    leave_one_out = {
        symbol: run_momentum(
            {key: value for key, value in bars.items() if key != symbol}, PRIMARY_CONFIG
        )
        for symbol in UNIVERSE
    }
    isolated = {
        "parent_only": run_momentum(
            {symbol: bars[symbol] for symbol in PARENT_SYMBOLS}, PRIMARY_CONFIG
        ),
        "extensions_only": run_momentum(
            {symbol: bars[symbol] for symbol in EXTENSION_SYMBOLS}, PRIMARY_CONFIG
        ),
    }
    walk_forward = walk_forward_analysis(primary)
    subperiods = subperiod_analysis(primary)
    all_variants = {PRIMARY_CONFIG.name: primary, **sensitivities}
    dependence = _dependence_analysis(primary, leave_one_out, isolated, all_variants, walk_forward)
    verdict = research_verdict(primary, dependence, walk_forward, all_variants)
    payload: dict[str, Any] = {
        "schema": "minimal_v1_cross_sectional_momentum_research_v1",
        "strategy_registration": dict(registration_identity),
        "dataset_identities": [dict(item) for item in dataset_identities],
        "data_quality": dict(validation),
        "timing_contract": {
            "signal": "trailing total price return excluding latest month",
            "signal_timestamp": "final common source-present session of month",
            "execution": "next common source-present session open",
            "signal_bar_execution": False,
            "stale_substitution": False,
            "forward_fill": False,
        },
        "cost_contract": {
            "fee_percent": float(BASELINE_FEE_PERCENT),
            "slippage_percent": float(BASELINE_SLIPPAGE_PERCENT),
            "fractional_shares": False,
            "insufficient_cash_behavior": "reduce_integer_quantity",
        },
        "primary": _portfolio_summary(primary),
        "benchmarks": {name: _portfolio_summary(run) for name, run in benchmarks.items()},
        "archived_ma_crossover_context": dict(archived_ma_context),
        "sensitivity_variants": {
            name: _portfolio_summary(run) for name, run in sensitivities.items()
        },
        "walk_forward": walk_forward,
        "subperiod_stability": subperiods,
        "leave_one_symbol_out": {
            symbol: _portfolio_summary(run) for symbol, run in leave_one_out.items()
        },
        "dataset_origin_analysis": {
            name: _portfolio_summary(run) for name, run in isolated.items()
        },
        "dependence_tests": dependence,
        "cost_impact": {
            "gross": _portfolio_summary(gross),
            "net_return_percent": _return(primary),
            "gross_return_percent": _return(gross),
            "return_cost_percentage_points": _return(gross) - _return(primary),
            "fees_bdt": primary.metrics["total_fees_bdt"],
            "slippage_bdt": primary.metrics["total_slippage_bdt"],
        },
        "research_verdict": verdict,
        "promotion_permission": False,
        "paper_campaign_eligibility": False,
        "execution_outside_authorized_historical_run": False,
        "qualification": "0/60",
    }
    ledger = [
        row
        for run in [primary, *sensitivities.values(), *benchmarks.values()]
        for row in run.ledger
    ]
    return ResearchBundle(payload=payload, ledger=ledger)
