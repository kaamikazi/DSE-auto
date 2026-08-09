from __future__ import annotations

import json
import math
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from statistics import mean
from typing import Any, cast

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    Order,
    PaperSession,
    ResearchDataset,
    StrategyRegistration,
    Transaction,
    ValidationCampaign,
)
from app.schemas.market import HistoricalBar, TimestampProvenance
from app.services.absolute_momentum_filter import (
    PRIMARY_CONFIG as ABSOLUTE_CONFIG,
)
from app.services.absolute_momentum_filter import (
    PRIMARY_PARAMETERS as ABSOLUTE_PARAMETERS,
)
from app.services.absolute_momentum_filter import (
    STRATEGY_IDENTITY as ABSOLUTE_IDENTITY,
)
from app.services.absolute_momentum_filter import (
    build_rebalance_plans as build_absolute_plans,
)
from app.services.absolute_momentum_filter import (
    code_hash as absolute_code_hash,
)
from app.services.absolute_momentum_filter import (
    parameter_hash as absolute_parameter_hash,
)
from app.services.cross_sectional_momentum import (
    PRIMARY_CONFIG as MOMENTUM_CONFIG,
)
from app.services.cross_sectional_momentum import (
    PRIMARY_PARAMETERS as MOMENTUM_PARAMETERS,
)
from app.services.cross_sectional_momentum import (
    STRATEGY_IDENTITY as MOMENTUM_IDENTITY,
)
from app.services.cross_sectional_momentum import (
    UNIVERSE as ORIGINAL_UNIVERSE,
)
from app.services.cross_sectional_momentum import (
    PortfolioRun,
    _period_returns,
    _portfolio_summary,
    _static_weight_plans,
    canonical_hash,
    simulate_plans,
    subperiod_analysis,
    walk_forward_analysis,
)
from app.services.cross_sectional_momentum import (
    build_rebalance_plans as build_momentum_plans,
)
from app.services.cross_sectional_momentum import (
    code_hash as momentum_code_hash,
)
from app.services.cross_sectional_momentum import (
    parameter_hash as momentum_parameter_hash,
)
from app.services.defensive_low_volatility import (
    PRIMARY_CONFIG as DEFENSIVE_CONFIG,
)
from app.services.defensive_low_volatility import (
    PRIMARY_PARAMETERS as DEFENSIVE_PARAMETERS,
)
from app.services.defensive_low_volatility import (
    STRATEGY_IDENTITY as DEFENSIVE_IDENTITY,
)
from app.services.defensive_low_volatility import (
    _add_defensive_metrics,
)
from app.services.defensive_low_volatility import (
    build_rebalance_plans as build_defensive_plans,
)
from app.services.defensive_low_volatility import (
    code_hash as defensive_code_hash,
)
from app.services.defensive_low_volatility import (
    parameter_hash as defensive_parameter_hash,
)
from app.services.historical_strategy_research import (
    BASELINE_FEE_PERCENT,
    BASELINE_SLIPPAGE_PERCENT,
    REGISTERED_PARAMETERS,
    STARTING_CAPITAL,
    _curve_metrics,
    run_symbol,
    sha256_file,
)
from app.services.research_governance import (
    PARAMETERS as MA_PARAMETERS,
)
from app.services.research_governance import (
    STRATEGY_ID as MA_STRATEGY_ID,
)
from app.services.research_governance import (
    STRATEGY_VERSION as MA_STRATEGY_VERSION,
)
from app.services.research_governance import (
    parameter_set_hash,
    strategy_code_hash,
)

MA_IDENTITY = f"{MA_STRATEGY_ID}@{MA_STRATEGY_VERSION}"
FROZEN_IDENTITIES = (
    MA_IDENTITY,
    MOMENTUM_IDENTITY,
    DEFENSIVE_IDENTITY,
    ABSOLUTE_IDENTITY,
)
EXPANDED_UNIVERSE = (
    *ORIGINAL_UNIVERSE,
    "HEIDELBCEM",
    "GPHISPAT",
    "GREENDELT",
    "PARAMOUNT",
    "OLYMPIC",
    "JAMUNABANK",
    "MJLBD",
    "CITYBANK",
    "AMCL(PRAN)",
    "DBH",
    "MARICO",
    "UNILEVERCL",
    "SUMITPOWER",
    "SQUARETEXT",
    "RELIANCINS",
)
APPROVED_DISPOSITIONS = {
    "tier_1_cross_source_confirmed",
    "tier_2_single_source_high_quality",
}
EXPECTED_DATASETS: tuple[dict[str, Any], ...] = (
    {
        "id": "ba5f2d99-6c66-4e37-ae31-d48c8ee47b15",
        "version": "gp-aci-bracbank-research-f24a48cb729e8a65",
        "sha256": "ddfeee6bbf5324f9f64fd604e9d4bfc7bd2a45ce2896e7b968037af58de04791",
        "source_sha256": "bc25849fa9c3d76435d7eb0088f5d4598a79d7928dee4c952a0130240dffae14",
        "symbols": list(ORIGINAL_UNIVERSE[:3]),
        "origin": "prior_parent",
    },
    {
        "id": "c6da44f7-b842-4d0a-a8e0-31bad7f96bea",
        "version": "batbc-squrpharma-t2-extension-5357a454f66e1ea7",
        "sha256": "4470633c8d62c627357e6c0a6472466142e7a6539f7100d9de677827dda8c882",
        "source_sha256": "4aff9723f470fe5cd6dbd83429f57dee9b5b8c96f714ea3d6f61923ad400dfeb",
        "symbols": list(ORIGINAL_UNIVERSE[3:5]),
        "origin": "prior_extension_1",
    },
    {
        "id": "8f43a2ed-7d78-4b57-8c30-1fb75db0e939",
        "version": "five-symbol-t2-extension-789c90798e1b4268",
        "sha256": "21b9dda627d7045fb42aadb397de6d09be0cf693d9d17931b11df72ace838f10",
        "source_sha256": "789c90798e1b42686494a6b2079372e6c97798c42eb183856be6ba01a2c1f869",
        "symbols": list(ORIGINAL_UNIVERSE[5:]),
        "origin": "prior_extension_2",
    },
    {
        "id": "305e25a5-4d45-4bae-aca8-3a118bf45cca",
        "version": "fifteen-symbol-t2-extension-e3164884ab0d39cb",
        "sha256": "ea7dc8b0c9048e763a8a338e3d8390bc76eb5ae52a863e4ca9b39b3c9ea5155a",
        "source_sha256": "e3164884ab0d39cbd345bb2a8e45a3d2a07eb5f78c1429235934603b2b52fc8c",
        "symbols": list(EXPANDED_UNIVERSE[10:]),
        "origin": "new_fifteen_extension",
    },
)


@dataclass(frozen=True)
class LoadedUniverse:
    bars: dict[str, list[HistoricalBar]]
    data_quality: dict[str, Any]
    datasets: list[dict[str, Any]]
    symbol_origins: dict[str, str]


def protected_counts(db: Session) -> dict[str, int]:
    return {
        "campaigns": int(db.scalar(select(func.count()).select_from(ValidationCampaign)) or 0),
        "paper_sessions": int(db.scalar(select(func.count()).select_from(PaperSession)) or 0),
        "orders": int(db.scalar(select(func.count()).select_from(Order)) or 0),
        "transactions": int(db.scalar(select(func.count()).select_from(Transaction)) or 0),
    }


def _dataset_path(dataset: ResearchDataset, repository_root: Path) -> Path:
    path = Path(dataset.normalized_file_path)
    return path if path.is_absolute() else repository_root / path


def _complete_lineage(row: Mapping[str, Any]) -> bool:
    return bool(
        row.get("selected_source")
        and row.get("source_lineage")
        and row.get("source_row_ids")
        and (row.get("raw_hashes") or row.get("raw_file_hashes"))
        and (row.get("audit_linkage") or row.get("audit_event_ids"))
    )


def load_expanded_universe(db: Session, repository_root: Path) -> LoadedUniverse:
    active = list(
        db.scalars(
            select(ResearchDataset)
            .where(ResearchDataset.status == "research_dataset_active")
            .order_by(ResearchDataset.created_at, ResearchDataset.id)
        )
    )
    if len(active) != len(EXPECTED_DATASETS):
        raise ValueError(f"Expected exactly four active datasets; found {len(active)}")
    if {item.id for item in active} != {str(item["id"]) for item in EXPECTED_DATASETS}:
        raise ValueError("Active dataset registry IDs differ from the frozen four-dataset set")

    bars: dict[str, list[HistoricalBar]] = {symbol: [] for symbol in EXPANDED_UNIVERSE}
    identities: list[dict[str, Any]] = []
    symbol_origins: dict[str, str] = {}
    full_keys: set[tuple[str, str, str]] = set()
    adjusted_keys: set[tuple[str, str]] = set()
    adjusted_counts: Counter[str] = Counter()
    unadjusted_counts: Counter[str] = Counter()
    disposition_counts: Counter[str] = Counter()
    total_rows = 0
    invalid_rows = 0
    incomplete_lineage_rows = 0

    for expected in EXPECTED_DATASETS:
        dataset = db.get(ResearchDataset, str(expected["id"]))
        if (
            dataset is None
            or dataset.name != expected["version"]
            or dataset.dataset_hash != expected["sha256"]
            or dataset.source_hash != expected["source_sha256"]
            or dataset.status != "research_dataset_active"
            or list(dataset.symbols) != list(expected["symbols"])
            or "immutable_lineage" not in set(dataset.data_types)
            or "adjusted_and_unadjusted" not in set(dataset.data_types)
        ):
            raise ValueError(f"Dataset identity or immutable contract mismatch: {expected['id']}")
        path = _dataset_path(dataset, repository_root)
        if not path.is_file() or sha256_file(path) != dataset.dataset_hash:
            raise ValueError(f"Dataset file hash mismatch: {expected['id']}")
        identities.append(
            {
                "registry_id": dataset.id,
                "version": dataset.name,
                "dataset_sha256": dataset.dataset_hash,
                "source_sha256": dataset.source_hash,
                "symbols": list(dataset.symbols),
                "origin": expected["origin"],
                "timestamp_trust": dataset.timestamp_trust,
                "audit_event_ids": list(dataset.audit_event_ids),
                "source_evidence_ids": list(dataset.source_evidence_ids),
                "path": path.resolve().relative_to(repository_root.resolve()).as_posix(),
            }
        )
        for symbol in dataset.symbols:
            if symbol in symbol_origins:
                raise ValueError(f"Symbol appears in more than one active dataset: {symbol}")
            symbol_origins[symbol] = str(expected["origin"])
        observed_symbols: set[str] = set()
        with path.open(encoding="utf-8") as handle:
            for raw in handle:
                if not raw.strip():
                    continue
                total_rows += 1
                row = cast(dict[str, Any], json.loads(raw))
                symbol = str(row.get("symbol"))
                day = str(row.get("date"))
                grain = str(row.get("adjustment_status"))
                disposition = str(row.get("final_disposition") or row.get("quality_tier") or "")
                observed_symbols.add(symbol)
                disposition_counts[disposition] += 1
                key = (symbol, day, grain)
                if key in full_keys:
                    raise ValueError(f"Duplicate active symbol/date/grain: {key}")
                full_keys.add(key)
                incomplete_lineage_rows += int(not _complete_lineage(row))
                try:
                    open_, high, low, close = (
                        float(row[field]) for field in ("open", "high", "low", "close")
                    )
                    valid = (
                        all(math.isfinite(value) for value in (open_, high, low, close))
                        and 0 < low <= min(open_, close) <= max(open_, close) <= high
                    )
                except (KeyError, TypeError, ValueError):
                    valid = False
                if not valid or grain not in {"adjusted", "unadjusted"}:
                    invalid_rows += 1
                    continue
                if disposition not in APPROVED_DISPOSITIONS:
                    raise ValueError(
                        f"Ineligible disposition reached active dataset: {disposition}"
                    )
                if grain == "unadjusted":
                    unadjusted_counts[symbol] += 1
                    continue
                adjusted_key = (symbol, day)
                if adjusted_key in adjusted_keys:
                    raise ValueError(f"Duplicate adjusted execution row: {adjusted_key}")
                adjusted_keys.add(adjusted_key)
                adjusted_counts[symbol] += 1
                bars[symbol].append(
                    HistoricalBar(
                        timestamp=datetime.combine(
                            date.fromisoformat(day), datetime.min.time(), UTC
                        ),
                        symbol=symbol,
                        open=Decimal(str(row["open"])),
                        high=Decimal(str(row["high"])),
                        low=Decimal(str(row["low"])),
                        close=Decimal(str(row["close"])),
                        volume=(
                            int(float(row["volume"])) if row.get("volume") is not None else None
                        ),
                        source=str(row["selected_source"]),
                        timestamp_provenance=TimestampProvenance.UNKNOWN,
                        quality_flags=[disposition, str(expected["origin"]), "adjusted_execution"],
                    )
                )
        if observed_symbols != set(dataset.symbols):
            raise ValueError(f"Dataset symbol inventory mismatch: {expected['id']}")

    if set(bars) != set(EXPANDED_UNIVERSE) or set(symbol_origins) != set(EXPANDED_UNIVERSE):
        raise ValueError("Expanded universe does not contain exactly the frozen 25 symbols")
    if invalid_rows or incomplete_lineage_rows or not all(bars.values()):
        raise ValueError("Active execution data failed validity, lineage, or completeness checks")
    for rows in bars.values():
        rows.sort(key=lambda item: item.timestamp)
    common_dates = sorted(
        set.intersection(*({item.timestamp.date() for item in rows} for rows in bars.values()))
    )
    if not common_dates:
        raise ValueError("The 25-symbol adjusted execution calendar has no common sessions")
    periods = {
        symbol: {
            "start": rows[0].timestamp.date().isoformat(),
            "end": rows[-1].timestamp.date().isoformat(),
            "adjusted_rows": len(rows),
        }
        for symbol, rows in sorted(bars.items())
    }
    data_quality = {
        "dataset_count": len(identities),
        "unique_symbol_count": len(bars),
        "symbols": list(EXPANDED_UNIVERSE),
        "total_approved_rows": total_rows,
        "total_adjusted_execution_rows": sum(adjusted_counts.values()),
        "total_unadjusted_reference_rows": sum(unadjusted_counts.values()),
        "adjusted_rows_by_symbol": dict(sorted(adjusted_counts.items())),
        "unadjusted_rows_by_symbol": dict(sorted(unadjusted_counts.items())),
        "effective_execution_periods": periods,
        "common_source_present_sessions": len(common_dates),
        "common_source_present_start": common_dates[0].isoformat(),
        "common_source_present_end": common_dates[-1].isoformat(),
        "complete_lineage": True,
        "full_grain_duplicates": 0,
        "adjusted_symbol_date_duplicates": 0,
        "invalid_rows_admitted": 0,
        "ineligible_rows_admitted": 0,
        "execution_grain": "adjusted",
        "unadjusted_execution_eligible": False,
        "excluded_dispositions": [
            "tier_3_research_only",
            "held_genuine_conflict",
            "held_lifecycle",
            "rejected_invalid",
            "rejected_duplicate_conflict",
        ],
        "approved_disposition_counts": dict(sorted(disposition_counts.items())),
    }
    return LoadedUniverse(bars, data_quality, identities, symbol_origins)


def validate_frozen_identities(db: Session, repository_root: Path) -> list[dict[str, Any]]:
    expected = (
        (
            MA_IDENTITY,
            MA_PARAMETERS,
            strategy_code_hash(),
            parameter_set_hash(MA_PARAMETERS),
        ),
        (
            MOMENTUM_IDENTITY,
            MOMENTUM_PARAMETERS,
            momentum_code_hash(repository_root),
            momentum_parameter_hash(),
        ),
        (
            DEFENSIVE_IDENTITY,
            DEFENSIVE_PARAMETERS,
            defensive_code_hash(repository_root),
            defensive_parameter_hash(),
        ),
        (
            ABSOLUTE_IDENTITY,
            ABSOLUTE_PARAMETERS,
            absolute_code_hash(repository_root),
            absolute_parameter_hash(),
        ),
    )
    output: list[dict[str, Any]] = []
    for identity, parameters, code_sha, parameter_sha in expected:
        strategy_id, version = identity.split("@", 1)
        registration = db.scalar(
            select(StrategyRegistration).where(
                StrategyRegistration.strategy_id == strategy_id,
                StrategyRegistration.version == version,
            )
        )
        if registration is None:
            raise ValueError(f"Frozen registration unavailable: {identity}")
        evidence = dict(registration.evidence or {})
        stored_parameter_hash = str(evidence.get("parameter_hash") or canonical_hash(parameters))
        if (
            registration.lifecycle_state != "research"
            or registration.code_hash != code_sha
            or registration.parameters != parameters
            or stored_parameter_hash != parameter_sha
            or evidence.get("promotion_authorized") is not False
        ):
            raise ValueError(f"Frozen strategy identity drift: {identity}")
        output.append(
            {
                "identity": identity,
                "registration_id": registration.id,
                "code_sha256": code_sha,
                "parameter_sha256": parameter_sha,
                "canonical_parameters": parameters,
                "lifecycle": registration.lifecycle_state,
                "historical_verdict_unchanged": evidence.get("research_verdict"),
                "promotion_permission": False,
                "campaign_eligibility": False,
                "external_execution_permission": False,
            }
        )
    return output


def _common_dates(bars: Mapping[str, Sequence[HistoricalBar]]) -> list[date]:
    return sorted(
        set.intersection(*({item.timestamp.date() for item in rows} for rows in bars.values()))
    )


def _restrict_to_dates(
    bars: Mapping[str, Sequence[HistoricalBar]], dates: set[date]
) -> dict[str, list[HistoricalBar]]:
    return {
        symbol: [item for item in rows if item.timestamp.date() in dates]
        for symbol, rows in bars.items()
    }


def _add_required_metrics(run: PortfolioRun) -> PortfolioRun:
    if "downside_volatility_percent" not in run.metrics:
        _add_defensive_metrics(run)
    return run


def _plans_run(
    bars: Mapping[str, Sequence[HistoricalBar]],
    plans: Sequence[Mapping[str, Any]],
    *,
    name: str,
    fee_percent: float = float(BASELINE_FEE_PERCENT),
    slippage_percent: float = float(BASELINE_SLIPPAGE_PERCENT),
) -> PortfolioRun:
    return _add_required_metrics(
        simulate_plans(
            bars,
            plans,
            name=name,
            fee_percent=fee_percent,
            slippage_percent=slippage_percent,
        )
    )


def _first_plan_dates(
    bars: Mapping[str, Sequence[HistoricalBar]],
    builder: Callable[[Mapping[str, Sequence[HistoricalBar]], Any], list[dict[str, Any]]],
    config: Any,
) -> dict[str, str]:
    output: dict[str, str] = {}
    for symbol, rows in sorted(bars.items()):
        plans = builder({symbol: rows}, config)
        if not plans:
            raise ValueError(f"No eligible frozen-strategy plan for {symbol}")
        output[symbol] = cast(date, plans[0]["signal_date"]).isoformat()
    return output


def strategy_eligibility_dates(
    bars: Mapping[str, Sequence[HistoricalBar]], identity: str
) -> dict[str, Any]:
    if identity == MA_IDENTITY:
        dates = {
            symbol: rows[REGISTERED_PARAMETERS["slow"] - 1].timestamp.date().isoformat()
            for symbol, rows in sorted(bars.items())
        }
    elif identity == MOMENTUM_IDENTITY:
        dates = _first_plan_dates(bars, build_momentum_plans, MOMENTUM_CONFIG)
    elif identity == DEFENSIVE_IDENTITY:
        dates = _first_plan_dates(bars, build_defensive_plans, DEFENSIVE_CONFIG)
    elif identity == ABSOLUTE_IDENTITY:
        dates = _first_plan_dates(bars, build_absolute_plans, ABSOLUTE_CONFIG)
    else:
        raise ValueError(f"Unsupported frozen strategy: {identity}")
    return {
        "per_symbol_first_eligible_signal": dates,
        "all_25_lookback_satisfied_from": max(dates.values()),
        "basis": "frozen primary lookback evaluated on each symbol's adjusted source-present rows",
    }


def _ma_run(
    bars: Mapping[str, Sequence[HistoricalBar]],
    *,
    name: str,
    signal_floor: date | None = None,
    fee_percent: Decimal = BASELINE_FEE_PERCENT,
    slippage_percent: Decimal = BASELINE_SLIPPAGE_PERCENT,
) -> PortfolioRun:
    prepared: dict[str, list[HistoricalBar]] = {}
    slow = REGISTERED_PARAMETERS["slow"]
    for symbol, rows in bars.items():
        values = list(rows)
        if signal_floor is not None:
            index = next(
                (i for i, item in enumerate(values) if item.timestamp.date() >= signal_floor),
                len(values),
            )
            if index < slow - 1:
                raise ValueError(f"Insufficient pre-window MA history for {symbol}")
            values = values[index - (slow - 1) :]
        prepared[symbol] = values
    results = {
        symbol: run_symbol(
            symbol,
            rows,
            parameters=REGISTERED_PARAMETERS,
            fee_percent=fee_percent,
            slippage_percent=slippage_percent,
        )
        for symbol, rows in prepared.items()
    }
    dates = _common_dates(prepared)
    if signal_floor is not None:
        dates = [day for day in dates if day >= signal_floor]
    curve_by_symbol = {
        symbol: {
            date.fromisoformat(str(point["timestamp"])[:10]): float(
                cast(float | int, point["equity"])
            )
            for point in result.equity_curve
        }
        for symbol, result in results.items()
    }
    curve: list[dict[str, object]] = [
        {
            "timestamp": day.isoformat(),
            "equity": mean(curve_by_symbol[symbol][day] for symbol in results),
        }
        for day in dates
    ]
    if len(curve) < 252:
        raise ValueError("MA validation window is too short")
    metrics = _curve_metrics(curve, float(STARTING_CAPITAL))
    metrics.update(
        {
            "turnover": mean(
                float(result.metrics["turnover_rate"] or 0) for result in results.values()
            ),
            "total_fees_bdt": mean(
                float(result.metrics["fee_impact_bdt"] or 0) for result in results.values()
            ),
            "total_slippage_bdt": mean(
                float(result.metrics["slippage_impact_bdt"] or 0) for result in results.values()
            ),
            "average_invested_exposure_percent": mean(
                float(result.metrics["exposure_percent"] or 0) for result in results.values()
            ),
        }
    )
    metrics["average_cash_exposure_percent"] = 100 - float(
        metrics["average_invested_exposure_percent"]
    )
    contributions = {
        symbol: (float(cast(float | int, result.metrics["final_equity"])) - float(STARTING_CAPITAL))
        / len(results)
        for symbol, result in results.items()
    }
    monthly = _period_returns(curve, "month")
    yearly = _period_returns(curve, "year")
    run = PortfolioRun(
        name=name,
        metrics=metrics,
        equity_curve=curve,
        ledger=[],
        rebalances=[],
        symbol_contribution=contributions,
        dataset_contribution={},
        monthly_returns=monthly,
        yearly_returns=yearly,
        time_held={},
        cash_periods=[],
    )
    return _add_required_metrics(run)


def _portfolio_strategy_run(
    identity: str,
    bars: Mapping[str, Sequence[HistoricalBar]],
    *,
    signal_floor: date | None,
    gross: bool = False,
) -> PortfolioRun:
    fee = 0.0 if gross else float(BASELINE_FEE_PERCENT)
    slip = 0.0 if gross else float(BASELINE_SLIPPAGE_PERCENT)
    if identity == MA_IDENTITY:
        return _ma_run(
            bars,
            name="ma_crossover_primary_20_50",
            signal_floor=signal_floor,
            fee_percent=Decimal(str(fee)),
            slippage_percent=Decimal(str(slip)),
        )
    builders: dict[str, tuple[Callable[..., list[dict[str, Any]]], Any, str]] = {
        MOMENTUM_IDENTITY: (build_momentum_plans, MOMENTUM_CONFIG, MOMENTUM_CONFIG.name),
        DEFENSIVE_IDENTITY: (build_defensive_plans, DEFENSIVE_CONFIG, DEFENSIVE_CONFIG.name),
        ABSOLUTE_IDENTITY: (build_absolute_plans, ABSOLUTE_CONFIG, ABSOLUTE_CONFIG.name),
    }
    builder, config, name = builders[identity]
    plans = builder(bars, config)
    if signal_floor is not None:
        plans = [plan for plan in plans if cast(date, plan["signal_date"]) >= signal_floor]
    run = _plans_run(
        bars,
        plans,
        name=name,
        fee_percent=fee,
        slippage_percent=slip,
    )
    if identity == ABSOLUTE_IDENTITY:
        run.metrics["all_cash_period_count"] = len(run.cash_periods)
    return run


def _benchmark_runs(
    bars: Mapping[str, Sequence[HistoricalBar]], start_signal: date
) -> dict[str, PortfolioRun]:
    equal = {symbol: 1 / len(bars) for symbol in bars}
    half = {symbol: 0.5 / len(bars) for symbol in bars}
    monthly = _static_weight_plans(
        bars,
        name="monthly_rebalanced_equal_weight",
        weights=equal,
        start_signal=start_signal,
        monthly=True,
    )
    configurations: dict[str, list[dict[str, Any]]] = {
        "equal_weight_buy_and_hold": _static_weight_plans(
            bars,
            name="equal_weight_buy_and_hold",
            weights=equal,
            start_signal=start_signal,
            monthly=False,
        ),
        "monthly_rebalanced_equal_weight": monthly,
        "quarterly_rebalanced_equal_weight": [
            plan for plan in monthly if cast(date, plan["signal_date"]).month in {3, 6, 9, 12}
        ],
        "half_equal_weight_equities_half_cash": _static_weight_plans(
            bars,
            name="half_equal_weight_equities_half_cash",
            weights=half,
            start_signal=start_signal,
            monthly=True,
        ),
    }
    return {name: _plans_run(bars, plans, name=name) for name, plans in configurations.items()}


def _summary(run: PortfolioRun, symbol_origins: Mapping[str, str]) -> dict[str, Any]:
    base = cast(dict[str, Any], _portfolio_summary(run))
    contributions = {key: float(value) for key, value in run.symbol_contribution.items()}
    absolute = {key: abs(value) for key, value in contributions.items()}
    absolute_total = sum(absolute.values())
    largest = max(absolute, key=absolute.__getitem__)
    by_dataset = {
        origin: sum(
            contributions.get(symbol, 0.0)
            for symbol, mapped_origin in symbol_origins.items()
            if mapped_origin == origin
        )
        for origin in sorted(set(symbol_origins.values()))
    }
    prior = sum(value for origin, value in by_dataset.items() if origin != "new_fifteen_extension")
    new = by_dataset.get("new_fifteen_extension", 0.0)
    base.update(
        {
            "dataset_contribution_bdt": by_dataset,
            "prior_ten_dataset_contribution_bdt": prior,
            "new_fifteen_extension_contribution_bdt": new,
            "largest_absolute_contributor": largest,
            "largest_absolute_contributor_share": (
                absolute[largest] / absolute_total if absolute_total else 0.0
            ),
        }
    )
    return base


def _leave_one_out(
    identity: str,
    bars: Mapping[str, Sequence[HistoricalBar]],
    signal_floor: date,
    full_return: float,
) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for excluded in sorted(bars):
        subset = {symbol: rows for symbol, rows in bars.items() if symbol != excluded}
        run = _portfolio_strategy_run(identity, subset, signal_floor=signal_floor)
        value = float(run.metrics["total_return_percent"])
        output[excluded] = {
            "return_percent": value,
            "change_from_full_percentage_points": value - full_return,
        }
    return output


def _window_result(
    identity: str,
    bars: Mapping[str, Sequence[HistoricalBar]],
    symbol_origins: Mapping[str, str],
    *,
    signal_floor: date | None,
) -> tuple[dict[str, Any], PortfolioRun]:
    primary = _portfolio_strategy_run(identity, bars, signal_floor=signal_floor)
    gross = _portfolio_strategy_run(identity, bars, signal_floor=signal_floor, gross=True)
    if primary.rebalances:
        first_signal = date.fromisoformat(str(primary.rebalances[0]["signal_date"]))
    else:
        first_signal = signal_floor or date.fromisoformat(str(primary.equity_curve[0]["timestamp"]))
    benchmarks = _benchmark_runs(bars, first_signal)
    gross_return = float(gross.metrics["total_return_percent"])
    net_return = float(primary.metrics["total_return_percent"])
    return (
        {
            "effective_start": str(primary.equity_curve[0]["timestamp"])[:10],
            "effective_end": str(primary.equity_curve[-1]["timestamp"])[:10],
            "primary": _summary(primary, symbol_origins),
            "gross_return_percent": gross_return,
            "cost_drag_percentage_points": gross_return - net_return,
            "costs_erase_most_of_gross": bool(
                gross_return > 0 and net_return <= gross_return * 0.5
            ),
            "benchmarks": {name: _summary(run, symbol_origins) for name, run in benchmarks.items()},
            "walk_forward": walk_forward_analysis(primary),
            "subperiods": subperiod_analysis(primary),
        },
        primary,
    )


def _assessment(
    common: Mapping[str, Any],
    natural: Mapping[str, Any],
    leave_one_out: Mapping[str, Any],
) -> dict[str, Any]:
    primary = cast(Mapping[str, Any], common["primary"])
    metrics = cast(Mapping[str, Any], primary["metrics"])
    walk = cast(Mapping[str, Any], common["walk_forward"])
    holdouts = cast(Sequence[Mapping[str, Any]], walk["partitions"])
    holdout_returns = [
        float(cast(Mapping[str, Any], item["holdout"])["metrics"]["total_return_percent"])
        for item in holdouts
    ]
    subperiods = cast(Sequence[Mapping[str, Any]], common["subperiods"])
    subperiod_returns = [float(item["metrics"]["total_return_percent"]) for item in subperiods]
    datasets = cast(Mapping[str, float], primary["dataset_contribution_bdt"])
    dataset_abs = sum(abs(value) for value in datasets.values())
    largest_dataset_share = max((abs(value) for value in datasets.values()), default=0.0) / (
        dataset_abs or 1.0
    )
    benchmark = cast(Mapping[str, Any], common["benchmarks"])["equal_weight_buy_and_hold"]
    benchmark_metrics = cast(Mapping[str, Any], benchmark["metrics"])
    common_return = float(metrics["total_return_percent"])
    natural_return = float(
        cast(Mapping[str, Any], natural["primary"])["metrics"]["total_return_percent"]
    )
    checks = {
        "combined_walk_forward_positive": float(
            cast(Mapping[str, Any], walk["combined_holdout"])["metrics"]["total_return_percent"]
        )
        > 0,
        "holdouts_not_catastrophically_unstable": min(holdout_returns) > -50,
        "not_dominated_by_one_symbol": float(primary["largest_absolute_contributor_share"]) <= 0.50,
        "not_dominated_by_one_dataset": largest_dataset_share <= 0.70,
        "not_dominated_by_one_subperiod": sum(value > 0 for value in subperiod_returns) >= 2,
        "costs_do_not_erase_most_of_gross": not bool(common["costs_erase_most_of_gross"]),
        "drawdown_defensible_relative_to_buy_hold": abs(float(metrics["maximum_drawdown_percent"]))
        <= abs(float(benchmark_metrics["maximum_drawdown_percent"])) * 1.25,
        "common_overlap_coherent": common_return == 0
        or natural_return == 0
        or (common_return > 0) == (natural_return > 0),
        "expanded_behavior_does_not_contradict_hypothesis": common_return > 0
        and float(metrics["sharpe_ratio"] or 0) > 0,
    }
    worst_leave_one_out = min(
        (float(item["return_percent"]) for item in leave_one_out.values()), default=0.0
    )
    if all(checks.values()):
        assessment = "survives_expanded_validation"
    elif (
        common_return <= 0
        and float(
            cast(Mapping[str, Any], walk["combined_holdout"])["metrics"]["total_return_percent"]
        )
        <= 0
    ):
        assessment = "fails_expanded_validation"
    else:
        assessment = "remains_inconclusive"
    return {
        "assessment": assessment,
        "criteria": checks,
        "holdout_returns_percent": holdout_returns,
        "subperiod_returns_percent": subperiod_returns,
        "largest_dataset_absolute_contribution_share": largest_dataset_share,
        "worst_leave_one_out_return_percent": worst_leave_one_out,
    }


def _comparison(original: Mapping[str, Any], expanded: Mapping[str, Any]) -> dict[str, Any]:
    original_metrics = cast(Mapping[str, Any], original["primary"])["metrics"]
    expanded_metrics = cast(Mapping[str, Any], expanded["primary"])["metrics"]
    original_walk = cast(Mapping[str, Any], original["walk_forward"])["combined_holdout"]["metrics"]
    expanded_walk = cast(Mapping[str, Any], expanded["walk_forward"])["combined_holdout"]["metrics"]
    original_sub = [
        float(item["metrics"]["total_return_percent"]) for item in original["subperiods"]
    ]
    expanded_sub = [
        float(item["metrics"]["total_return_percent"]) for item in expanded["subperiods"]
    ]
    return {
        "original_10": original["primary"],
        "expanded_25": expanded["primary"],
        "return_change_percentage_points": float(expanded_metrics["total_return_percent"])
        - float(original_metrics["total_return_percent"]),
        "drawdown_change_percentage_points": float(expanded_metrics["maximum_drawdown_percent"])
        - float(original_metrics["maximum_drawdown_percent"]),
        "cost_change_bdt": float(expanded_metrics["total_fees_bdt"])
        + float(expanded_metrics["total_slippage_bdt"])
        - float(original_metrics["total_fees_bdt"])
        - float(original_metrics["total_slippage_bdt"]),
        "combined_holdout_change_percentage_points": float(expanded_walk["total_return_percent"])
        - float(original_walk["total_return_percent"]),
        "subperiod_return_range_change_percentage_points": (max(expanded_sub) - min(expanded_sub))
        - (max(original_sub) - min(original_sub)),
        "largest_contributor_share_change": float(
            expanded["primary"]["largest_absolute_contributor_share"]
        )
        - float(original["primary"]["largest_absolute_contributor_share"]),
    }


def build_expanded_validation(
    db: Session, repository_root: Path, *, git_commit: str
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    before = protected_counts(db)
    identities = validate_frozen_identities(db, repository_root)
    loaded = load_expanded_universe(db, repository_root)
    common_dates = _common_dates(loaded.bars)
    common_set = set(common_dates)
    common_bars = _restrict_to_dates(loaded.bars, common_set)
    original_bars = {symbol: common_bars[symbol] for symbol in ORIGINAL_UNIVERSE}
    original_origins = {symbol: loaded.symbol_origins[symbol] for symbol in ORIGINAL_UNIVERSE}
    strategies: dict[str, Any] = {}
    comparison_rows: list[dict[str, Any]] = []

    for identity in FROZEN_IDENTITIES:
        # The frozen multi-symbol implementations use the common source-present
        # calendar. Eligibility must therefore be measured on that same execution
        # clock, not on denser per-symbol calendars that the strategy never sees.
        eligibility = strategy_eligibility_dates(common_bars, identity)
        floor = date.fromisoformat(str(eligibility["all_25_lookback_satisfied_from"]))
        natural, _ = _window_result(
            identity,
            common_bars,
            loaded.symbol_origins,
            # The other three frozen engines naturally begin at their first
            # eligible rebalance. MA is a continuous state model, so apply the
            # equivalent 50-session eligibility floor explicitly.
            signal_floor=floor if identity == MA_IDENTITY else None,
        )
        common, common_run = _window_result(
            identity,
            common_bars,
            loaded.symbol_origins,
            signal_floor=floor,
        )
        original, _ = _window_result(
            identity,
            original_bars,
            original_origins,
            signal_floor=floor,
        )
        full_return = float(common_run.metrics["total_return_percent"])
        leave_one_out = _leave_one_out(identity, common_bars, floor, full_return)
        assessment = _assessment(common, natural, leave_one_out)
        strategies[identity] = {
            "registration": next(item for item in identities if item["identity"] == identity),
            "dated_eligibility": eligibility,
            "natural_eligibility_window": natural,
            "common_25_symbol_overlap_window": common,
            "common_window_original_10_control": original,
            "original_10_vs_expanded_25": _comparison(original, common),
            "leave_one_symbol_out": leave_one_out,
            "expanded_validation_assessment": assessment,
        }
        for window_name, result, universe_size in (
            ("natural_eligibility", natural, 25),
            ("common_overlap", common, 25),
            ("common_overlap_original_10", original, 10),
        ):
            comparison_rows.append(
                _comparison_row(identity, window_name, "strategy", result["primary"], universe_size)
            )
            for benchmark_name, benchmark in result["benchmarks"].items():
                comparison_rows.append(
                    _comparison_row(
                        identity,
                        window_name,
                        benchmark_name,
                        benchmark,
                        universe_size,
                    )
                )

    survivors = [
        identity
        for identity, result in strategies.items()
        if result["expanded_validation_assessment"]["assessment"] == "survives_expanded_validation"
    ]
    if survivors:
        strongest = max(
            survivors,
            key=lambda identity: (
                sum(
                    bool(value)
                    for value in strategies[identity]["expanded_validation_assessment"][
                        "criteria"
                    ].values()
                ),
                float(
                    strategies[identity]["common_25_symbol_overlap_window"]["walk_forward"][
                        "combined_holdout"
                    ]["metrics"]["total_return_percent"]
                ),
                -abs(
                    float(
                        strategies[identity]["common_25_symbol_overlap_window"]["primary"][
                            "metrics"
                        ]["maximum_drawdown_percent"]
                    )
                ),
                identity,
            ),
        )
        final_decision = "FORWARD_PAPER_CANDIDATE_EXISTS"
    else:
        strongest = None
        final_decision = "NO_FORWARD_PAPER_CANDIDATE"

    after = protected_counts(db)
    if after != before:
        raise RuntimeError("Historical validation changed protected operational entities")
    payload = {
        "schema": "minimal_v1_expanded_strategy_validation_v1",
        "git_commit": git_commit,
        "qualification": "0/60",
        "purpose": "frozen-strategy_25-symbol_historical_validation",
        "frozen_strategy_identities": identities,
        "active_data": {
            "datasets": loaded.datasets,
            "quality": loaded.data_quality,
            "common_calendar": {
                "sessions": len(common_dates),
                "start": common_dates[0].isoformat(),
                "end": common_dates[-1].isoformat(),
                "rule": "intersection of adjusted source-present dates; no forward fill",
            },
        },
        "execution_contract": {
            "fee_percent": float(BASELINE_FEE_PERCENT),
            "slippage_percent": float(BASELINE_SLIPPAGE_PERCENT),
            "whole_share_sizing": True,
            "leverage": False,
            "short_selling": False,
            "next_source_present_open": True,
            "same_bar_execution": False,
            "stale_substitution": False,
            "forward_fill": False,
        },
        "strategies": strategies,
        "final_decision": {
            "decision": final_decision,
            "strongest_candidate": strongest,
            "promotion_performed": False,
            "paper_session_created": False,
            "strategy_discovery_frozen": final_decision == "NO_FORWARD_PAPER_CANDIDATE",
        },
        "important_limitation": (
            "The fifteen added symbols were selected independently of strategy performance, "
            "which makes this stronger historical evidence. However, this is NOT true future "
            "out-of-sample validation. It still uses historical data and cannot substitute for "
            "forward paper validation or prove profitability."
        ),
        "safety": {
            "trading_mode": "paper",
            "live_trading_enabled": False,
            "broker_adapter": "disabled",
            "protected_entity_counts_before": before,
            "protected_entity_counts_after": after,
            "operational_mutations": 0,
            "strategy_registration_mutations": 0,
            "historical_verdict_mutations": 0,
        },
        "complexity": {
            "tables": 0,
            "migrations": 0,
            "states": 0,
            "audit_event_types": 0,
            "cli_commands": 0,
            "generic_abstractions": 0,
            "wrappers": 0,
            "report_formats": 0,
            "dedicated_validation_entrypoints": 1,
        },
    }
    return payload, comparison_rows


def _comparison_row(
    identity: str,
    window: str,
    comparison: str,
    summary: Mapping[str, Any],
    universe_size: int,
) -> dict[str, Any]:
    metrics = cast(Mapping[str, Any], summary["metrics"])
    monthly = cast(Mapping[str, float], summary["monthly_returns_percent"])
    return {
        "strategy": identity,
        "window": window,
        "comparison": comparison,
        "universe_size": universe_size,
        "effective_observations": len(monthly),
        "net_return_percent": metrics["total_return_percent"],
        "annualized_return_percent": metrics["annualized_return_percent"],
        "maximum_drawdown_percent": metrics["maximum_drawdown_percent"],
        "drawdown_duration_days": metrics["drawdown_duration_days"],
        "volatility_percent": metrics["volatility_percent"],
        "downside_volatility_percent": metrics["downside_volatility_percent"],
        "sharpe_ratio": metrics["sharpe_ratio"],
        "worst_rolling_12_month_return_percent": metrics["worst_rolling_12_month_return_percent"],
        "turnover": metrics["turnover"],
        "fees_bdt": metrics["total_fees_bdt"],
        "slippage_bdt": metrics["total_slippage_bdt"],
        "invested_exposure_percent": metrics["average_invested_exposure_percent"],
        "cash_exposure_percent": metrics["average_cash_exposure_percent"],
        "winning_months": summary["winning_months"],
        "losing_months": summary["losing_months"],
        "largest_contributor": summary["largest_absolute_contributor"],
        "largest_contributor_share": summary["largest_absolute_contributor_share"],
    }


__all__ = [
    "EXPECTED_DATASETS",
    "EXPANDED_UNIVERSE",
    "FROZEN_IDENTITIES",
    "LoadedUniverse",
    "build_expanded_validation",
    "load_expanded_universe",
    "protected_counts",
    "strategy_eligibility_dates",
    "validate_frozen_identities",
]
