from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.backtesting.engine import run_backtest
from app.data.providers.mock import MockProvider
from app.schemas.trading import BacktestRequest

STRATEGY_ID = "ma_crossover"
STRATEGY_VERSION = "1.0.0"
PARAMETERS: dict[str, float | int] = {"fast": 20, "slow": 50}
MINIMUM_SAMPLE_SIZE = 252


def canonical_hash(payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def parameter_set_hash(parameters: dict[str, float | int] | None = None) -> str:
    return canonical_hash(parameters or PARAMETERS)


def strategy_code_hash(engine_path: Path | None = None) -> str:
    path = engine_path or Path(__file__).resolve().parents[1] / "backtesting" / "engine.py"
    payload = path.read_bytes() + b"\0" + STRATEGY_ID.encode() + b"\0" + STRATEGY_VERSION.encode()
    return hashlib.sha256(payload).hexdigest()


def _request(*, fee: str = "0.4", slippage: str = "0.1") -> BacktestRequest:
    return BacktestRequest(
        symbol="GP",
        strategy="ma_crossover",
        parameters=PARAMETERS,
        starting_capital=Decimal("1000000"),
        fee_percent=Decimal(fee),
        slippage_percent=Decimal(slippage),
        minimum_quantity=1,
    )


def build_ma_crossover_evidence() -> dict[str, Any]:
    bars = MockProvider().get_history("GP", date(2024, 1, 1), date(2025, 12, 31))
    first = run_backtest(bars, _request())
    replay = run_backtest(bars, _request())
    transaction_cost_sensitivity = []
    for fee in ("0", "0.4", "0.75", "1.0"):
        result = run_backtest(bars, _request(fee=fee))
        transaction_cost_sensitivity.append(
            {
                "fee_percent": fee,
                "final_equity": result.metrics["final_equity"],
                "total_return_percent": result.metrics["total_return_percent"],
            }
        )
    slippage_sensitivity = []
    for slippage in ("0", "0.1", "0.25", "0.5"):
        result = run_backtest(bars, _request(slippage=slippage))
        slippage_sensitivity.append(
            {
                "slippage_percent": slippage,
                "final_equity": result.metrics["final_equity"],
                "total_return_percent": result.metrics["total_return_percent"],
            }
        )
    regimes = []
    thirds = (
        bars[: len(bars) // 3],
        bars[len(bars) // 3 : 2 * len(bars) // 3],
        bars[2 * len(bars) // 3 :],
    )
    for label, regime_bars in zip(("early", "middle", "late"), thirds, strict=True):
        result = run_backtest(regime_bars, _request())
        regimes.append(
            {
                "label": label,
                "bars": len(regime_bars),
                "total_return_percent": result.metrics["total_return_percent"],
                "maximum_drawdown_percent": result.metrics["maximum_drawdown_percent"],
                "turnover_rate": result.metrics["turnover_rate"],
            }
        )
    engine_source = (Path(__file__).resolve().parents[1] / "backtesting" / "engine.py").read_text(
        encoding="utf-8"
    )
    return {
        "strategy_id": STRATEGY_ID,
        "strategy_name": "Moving Average Crossover",
        "version": STRATEGY_VERSION,
        "lifecycle_target": "research",
        "promotion_authorized": False,
        "code_hash": strategy_code_hash(),
        "code_hash_method": "SHA-256(engine.py bytes + NUL + strategy_id + NUL + version)",
        "parameter_set": PARAMETERS,
        "parameter_set_hash": parameter_set_hash(),
        "description": "Long-only moving-average state model using a 20-bar fast and 50-bar slow average.",
        "expected_behavior": "Seek sustained upward trends; exit after the fast average no longer exceeds the slow average.",
        "expected_turnover": {
            "synthetic_replay_turnover_rate": first.metrics["turnover_rate"],
            "classification": "lower than the other active-signal source strategies in the existing synthetic reports",
            "real_market_expectation_verified": False,
        },
        "required_data": {
            "fields": ["symbol", "timestamp", "open", "high", "low", "close", "volume", "source"],
            "minimum_ordered_daily_bars": MINIMUM_SAMPLE_SIZE,
            "timestamp_provenance": ["operator_attested", "exchange_verified"],
            "maximum_staleness_seconds_during_session": 30,
            "adjusted_corporate_actions_required": True,
        },
        "liquidity_assumptions": {
            "maximum_participation_percent_proposed": 10,
            "minimum_daily_volume_is_unapproved": True,
            "partial_fills_required": True,
        },
        "failure_conditions": [
            "missing or unordered bars",
            "untrusted or stale timestamp",
            "corporate-action discontinuity not reviewed",
            "insufficient liquidity",
            "implementation or parameter hash drift",
            "invalid audit, reconciliation, or emergency-stop state",
        ],
        "risk_interactions": [
            "position and concentration caps",
            "cash reserve and exposure caps",
            "daily loss and campaign drawdown stops",
            "liquidity participation and stale-data rejection",
            "strategy suspension and repeated-loss cooldown",
        ],
        "deterministic_replay": {
            "passed": asdict(first) == asdict(replay),
            "input_bars": len(bars),
            "input_source": "mock/synthetic",
            "result_hash": canonical_hash(asdict(first)),
        },
        "implementation_verification": {
            "next_bar_execution_present": "desired[idx - 1]" in engine_source,
            "market_orders_used": False,
            "short_selling": False,
            "leverage": False,
        },
        "look_ahead_bias": {
            "status": "controlled_in_implementation",
            "evidence": "signals at index n are applied to the next bar through desired[idx - 1]",
            "independent_reviewed": False,
        },
        "survivorship_bias": {
            "status": "not_resolved",
            "disclosure": "The current single-symbol synthetic fixture does not model delistings, index membership changes, or failed issuers.",
        },
        "backtest": {"metrics": first.metrics, "trades": [asdict(item) for item in first.trades]},
        "walk_forward": [asdict(item) for item in first.walk_forward],
        "parameter_sensitivity": [asdict(item) for item in first.sensitivity],
        "transaction_cost_sensitivity": transaction_cost_sensitivity,
        "slippage_sensitivity": slippage_sensitivity,
        "parameter_instability": {
            "variants": len(first.sensitivity),
            "results": [asdict(item) for item in first.sensitivity],
            "stable_for_real_market": False,
        },
        "regime_analysis": regimes,
        "concentration_analysis": {
            "single_symbol_fixture": True,
            "potential_invested_concentration_percent": 100,
            "campaign_limit_proposed_percent": 10,
            "campaign_limit_approved": False,
        },
        "drawdown_analysis": {
            "maximum_drawdown_percent": first.metrics["maximum_drawdown_percent"],
            "duration_bars": first.metrics["drawdown_duration_bars"],
            "real_market_calibrated": False,
        },
        "risk_review": {
            "reviewer": "operator-reviewer",
            "reviewer_is_operator": True,
            "independent": False,
            "classification": "non-independent research review",
            "risk_limits_approved": False,
            "result": "not eligible for paper_candidate or paper_active",
        },
        "minimum_sample_size_policy": {
            "minimum_daily_bars_per_symbol": MINIMUM_SAMPLE_SIZE,
            "minimum_walk_forward_windows": 3,
            "multi_symbol_real_data_required_before_candidate_review": True,
            "approved": False,
        },
        "synthetic_data_limitation": "All numerical results in this evidence use deterministic mock/synthetic data and are not real-market, profitability, or qualification evidence.",
        "missing_evidence": [
            "independent strategy risk review",
            "reviewed real GP/ACI/BRACBANK and DSEX data",
            "multi-symbol sample-size evidence",
            "survivorship-bias-controlled universe",
            "authoritative DSE rules and transaction costs",
            "approved risk limits",
            "paper_candidate and paper_active approvals",
        ],
        "no_profit_guarantee": "Historical, synthetic, and paper results do not guarantee profit.",
    }


def build_risk_limit_review() -> dict[str, Any]:
    def row(
        item: str,
        value: object,
        unit: str,
        rationale: str,
        effect: str,
        alternative: str,
        failure: str,
        authoritative: bool,
        scenario: str,
    ) -> dict[str, Any]:
        return {
            "item": item,
            "proposed_value": value,
            "unit": unit,
            "rationale": rationale,
            "effect": effect,
            "conservative_alternative": alternative,
            "failure_behavior": failure,
            "authoritative_evidence_required": authoritative,
            "scenario": scenario,
            "approved": False,
        }

    return {
        "classification": "standalone_unapproved_risk_limit_review",
        "limits": [
            row(
                "maximum_position_size",
                10,
                "% equity",
                "proposed concentration bound",
                "caps one holding",
                "5% equity",
                "block proposal",
                False,
                "A GP proposal taking exposure to 11% is rejected.",
            ),
            row(
                "maximum_symbol_concentration",
                10,
                "% equity",
                "same proposed per-symbol bound",
                "limits single-name loss",
                "5% equity",
                "block proposal",
                False,
                "Existing 8% plus a 3% order is rejected.",
            ),
            row(
                "maximum_sector_concentration",
                None,
                "% equity",
                "not yet proposed",
                "controls correlated holdings",
                "block sector additions until a cap is approved",
                "fail closed",
                False,
                "GP plus another telecom cannot be assessed without a cap.",
            ),
            row(
                "maximum_daily_loss",
                1,
                "% equity",
                "operator proposal",
                "stops new risk after loss",
                "0.5% equity",
                "pause session",
                False,
                "At a 1% combined realized/unrealized loss, new proposals stop.",
            ),
            row(
                "maximum_campaign_drawdown",
                8,
                "% from peak",
                "hard-pause proposal",
                "halts campaign deterioration",
                "5% hard pause",
                "hard pause and incident",
                False,
                "An 8% peak-to-trough decline pauses the campaign.",
            ),
            row(
                "maximum_order_value",
                None,
                "BDT or % equity",
                "not yet proposed",
                "limits single-order shock",
                "5% equity",
                "block until defined",
                False,
                "A BDT 150,000 proposal cannot pass without an approved cap.",
            ),
            row(
                "liquidity_participation",
                10,
                "% observed volume",
                "existing conservative paper default",
                "limits market impact",
                "5% volume",
                "partial fill or reject",
                True,
                "A 2,000-share order with 10,000 volume fills at most 1,000.",
            ),
            row(
                "stale_data_limit",
                30,
                "seconds",
                "existing application default",
                "blocks outdated quotes",
                "15 seconds",
                "block proposal and incident",
                True,
                "A quote aged 31 seconds is rejected.",
            ),
            row(
                "provider_disagreement",
                1,
                "% price",
                "existing application default",
                "blocks conflicting sources",
                "0.5%",
                "block proposal",
                True,
                "Quotes differing by 1.1% cannot authorize a proposal.",
            ),
            row(
                "turnover_limit",
                None,
                "portfolio turns/period",
                "not yet proposed",
                "controls fee drag and churn",
                "block strategy until defined",
                "fail closed",
                False,
                "A high-churn replay cannot be approved without a cap.",
            ),
            row(
                "loss_cooldown",
                3,
                "consecutive losses",
                "existing risk-engine default",
                "prevents repeated loss escalation",
                "2 losses",
                "suspend new proposals",
                False,
                "After three losses, the next signal is blocked.",
            ),
            row(
                "emergency_stop_triggers",
                ["manual stop", "invalid audit", "failed reconciliation", "critical incident"],
                "trigger set",
                "operator safety proposal",
                "stops all paper operations",
                "include data/provider and database failure triggers",
                "emergency stop",
                False,
                "An invalid audit chain blocks every operation.",
            ),
        ],
        "activation_authorized": False,
        "no_financial_advice": True,
    }


def build_rule_verification_review() -> dict[str, Any]:
    def row(
        item: str, value: object, confidence: str, consequence: str, fallback: str
    ) -> dict[str, Any]:
        return {
            "item": item,
            "current_draft_value": value,
            "evidence_source": "internal assumed draft; authoritative DSE source pending",
            "verification_status": "assumed",
            "confidence": confidence,
            "operational_consequence": consequence,
            "conservative_fallback": fallback,
            "approved": False,
        }

    return {
        "rule_set_id": "7b200921-840c-4586-8554-9cb98d0aee32",
        "version": "dse-paper-rules-v1-draft",
        "integrity_hash": "40de3fec28aac8439afc6512c78cbed1d9ce90dc3889b0e94ee96e7af87803f2",
        "active": False,
        "items": [
            row(
                "timezone",
                "Asia/Dhaka",
                "medium",
                "normalizes timestamps",
                "block on timezone ambiguity",
            ),
            row(
                "weekly_trading_days",
                "Sunday-Thursday",
                "low",
                "determines eligible dates",
                "require daily operator confirmation",
            ),
            row(
                "market_sessions",
                "10:00-14:30 configurable",
                "low",
                "bounds proposal window",
                "keep session closed until confirmed",
            ),
            row(
                "auction_periods",
                "09:55-10:00; 14:30-14:35 placeholders",
                "low",
                "changes fill eligibility",
                "no auction fills",
            ),
            row(
                "emergency_closures",
                "operator calendar; none preloaded",
                "unknown",
                "blocks closed dates",
                "block until closure status reviewed",
            ),
            row(
                "tick_sizes",
                "flat BDT 0.10 placeholder",
                "low",
                "normalizes limit prices",
                "reject if authoritative tick unavailable",
            ),
            row(
                "price_bands",
                "10% placeholder",
                "low",
                "blocks limit-band fills",
                "reject when band cannot be verified",
            ),
            row(
                "suspensions",
                "block proposals and fills",
                "medium",
                "prevents suspended-symbol activity",
                "block on unknown status",
            ),
            row(
                "minimum_quantities",
                "1 share software minimum",
                "low",
                "rounds order quantity",
                "block until lot rule confirmed",
            ),
            row(
                "liquidity_limits",
                "1,000 daily volume; 10% participation",
                "low",
                "caps simulated fills",
                "5% participation and block unknown volume",
            ),
            row(
                "settlement",
                "T+2 unverified",
                "low",
                "controls settled-share availability",
                "block sells without settled quantity",
            ),
            row(
                "expiry",
                "end of session",
                "medium",
                "expires pending paper orders",
                "expire earlier on uncertainty",
            ),
            row(
                "corporate_actions",
                "operator review before adjustment",
                "medium",
                "prevents silent position changes",
                "pause symbol",
            ),
            row("short_selling", "blocked", "high", "prevents negative holdings", "blocked"),
            row("leverage", "blocked", "high", "prevents borrowed exposure", "blocked"),
            row("market_orders", "blocked", "high", "requires limit prices", "blocked"),
        ],
        "authoritative_claim": False,
    }


def build_fee_verification_review() -> dict[str, Any]:
    def row(
        item: str,
        value: object,
        unit: str,
        evidence: str,
        alternative: str,
        impact: str,
    ) -> dict[str, Any]:
        return {
            "item": item,
            "draft_value": value,
            "unit": unit,
            "effective_date": "2026-07-16",
            "broker_account_applicability": "generic paper placeholder; no broker/account approved",
            "evidence_required": evidence,
            "confidence": "low" if value is not None else "unknown",
            "conservative_alternative": alternative,
            "sensitivity_impact": impact,
            "approved": False,
        }

    return {
        "fee_profile_id": "42995826-2488-4d59-a466-b67202c4f88e",
        "version": "1.0-draft",
        "integrity_hash": "de75af79e34a049776389abb3277005a4edcfaf516d52e57c4da609ec1acd249",
        "active": False,
        "items": [
            row(
                "buy_brokerage",
                0.5,
                "% gross",
                "broker schedule",
                "0.75%",
                "higher cost lowers returns",
            ),
            row(
                "sell_brokerage",
                0.5,
                "% gross",
                "broker schedule",
                "0.75%",
                "higher cost lowers proceeds",
            ),
            row(
                "minimum_brokerage",
                10,
                "BDT/order",
                "broker schedule",
                "BDT 20",
                "larger effect on small orders",
            ),
            row(
                "exchange_charge",
                0.02,
                "% gross",
                "exchange/contract schedule",
                "0.04%",
                "adds both-side drag",
            ),
            row(
                "regulatory_charge",
                0.01,
                "% gross",
                "regulatory/contract schedule",
                "0.02%",
                "adds both-side drag",
            ),
            row(
                "buy_tax",
                0.0,
                "% gross",
                "tax advice and broker statement",
                "block cost approval until confirmed",
                "currently excluded",
            ),
            row(
                "sell_tax",
                0.1,
                "% gross",
                "tax advice and broker statement",
                "0.2%",
                "reduces proceeds",
            ),
            row(
                "settlement_charge",
                5,
                "BDT/order",
                "CDBL/broker schedule",
                "BDT 10",
                "fixed per-order drag",
            ),
            row(
                "account_specific_charge",
                0,
                "BDT",
                "actual account statement",
                "exclude account from evidence",
                "currently excluded",
            ),
            row(
                "flat_buy_fee",
                0,
                "BDT/order",
                "actual broker schedule",
                "block if unknown",
                "currently excluded",
            ),
            row(
                "flat_sell_fee",
                0,
                "BDT/order",
                "actual broker schedule",
                "block if unknown",
                "currently excluded",
            ),
            row(
                "other_deductions",
                None,
                "BDT or %",
                "itemized broker statement",
                "block qualifying cost evidence",
                "unknown deductions omitted",
            ),
        ],
        "verified_total_presented": False,
        "authoritative_claim": False,
    }
