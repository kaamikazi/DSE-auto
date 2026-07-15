from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.services.research_governance import (  # noqa: E402
    build_fee_verification_review,
    build_ma_crossover_evidence,
    build_risk_limit_review,
    build_rule_verification_review,
)

DECISION_AUDIT_EVENT_IDS = [
    "REMOVED-OPERATIONAL-AUDIT-ID",
    "REMOVED-OPERATIONAL-AUDIT-ID",
    "REMOVED-OPERATIONAL-AUDIT-ID",
    "REMOVED-OPERATIONAL-AUDIT-ID",
    "REMOVED-OPERATIONAL-AUDIT-ID",
    "REMOVED-OPERATIONAL-AUDIT-ID",
    "REMOVED-OPERATIONAL-AUDIT-ID",
    "REMOVED-OPERATIONAL-AUDIT-ID",
    "REMOVED-OPERATIONAL-AUDIT-ID",
    "REMOVED-OPERATIONAL-AUDIT-ID",
    "REMOVED-OPERATIONAL-AUDIT-ID",
]


def _write_json(path: Path, payload: dict[str, Any]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(payload, indent=2, sort_keys=True, default=str).encode()
    path.write_bytes(encoded)
    return hashlib.sha256(encoded).hexdigest()


def _write_text(path: Path, text: str) -> str:
    encoded = text.encode()
    path.write_bytes(encoded)
    return hashlib.sha256(encoded).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate research-only pre-campaign evidence"
    )
    parser.add_argument("--output", type=Path, default=ROOT / "reports" / "governance")
    parser.add_argument(
        "--registration-id", default="REMOVED-OPERATIONAL-REGISTRATION-ID"
    )
    args = parser.parse_args()
    strategy = build_ma_crossover_evidence()
    risk = build_risk_limit_review()
    rules = build_rule_verification_review()
    fees = build_fee_verification_review()
    strategy_path = args.output / "ma_crossover_1_0_0_governance.json"
    risk_path = args.output / "risk_limit_approval_pack.json"
    rule_path = args.output / "rule_verification_table.json"
    fee_path = args.output / "fee_verification_table.json"
    approval_path = args.output / "pre_campaign_approval_pack.json"
    approval = {
        "classification": "research_only_pre_campaign_approval_pack",
        "decision_audit_event_ids": DECISION_AUDIT_EVENT_IDS,
        "blanket_authorization": False,
        "decisions": {
            "strategy_registration": "ma_crossover@1.0.0 research only",
            "rule_activation": "rejected",
            "fee_activation": "rejected",
            "strategy_promotion": "rejected",
            "campaign_creation": "rejected",
            "symbols": ["GP", "ACI", "BRACBANK"],
            "symbol_scope": [
                "data collection",
                "import validation",
                "research",
                "backtesting",
            ],
            "simulated_capital_bdt": 1000000,
        },
        "strategy_registration": {
            "id": args.registration_id,
            "strategy_id": "ma_crossover",
            "version": "1.0.0",
            "state": "research",
            "promotion_authorized": False,
            "code_hash": strategy["code_hash"],
            "parameter_set_hash": strategy["parameter_set_hash"],
            "missing_evidence": strategy["missing_evidence"],
        },
        "rule_review": rules,
        "fee_review": fees,
        "risk_review": risk,
        "reviewer": {"identity": "operator-reviewer", "also_operator": True, "independent": False},
        "campaign": {
            "name": "Reference Portfolio 60-Day Paper Validation",
            "created": False,
            "active": False,
            "qualification": "0/60",
            "start_date": None,
            "strategies": [],
            "symbols_approved_for_campaign": [],
        },
        "next_approvals_required": [
            "independent strategy risk review",
            "risk limits",
            "strategy paper_candidate promotion",
            "strategy paper_active promotion",
            "authoritative rule verification and activation",
            "authoritative fee verification and activation",
            "campaign symbols",
            "campaign creation",
        ],
        "no_orders_or_fills_authorized": True,
        "no_profit_guarantee": True,
    }
    result = {
        "strategy": {
            "path": str(strategy_path),
            "sha256": _write_json(strategy_path, strategy),
        },
        "risk": {"path": str(risk_path), "sha256": _write_json(risk_path, risk)},
        "rules": {"path": str(rule_path), "sha256": _write_json(rule_path, rules)},
        "fees": {"path": str(fee_path), "sha256": _write_json(fee_path, fees)},
        "approval": {
            "path": str(approval_path),
            "sha256": _write_json(approval_path, approval),
        },
    }
    strategy_md = args.output / "ma_crossover_1_0_0_governance.md"
    risk_md = args.output / "risk_limit_approval_pack.md"
    approval_md = args.output / "pre_campaign_approval_pack.md"
    result["strategy_markdown"] = {
        "path": str(strategy_md),
        "sha256": _write_text(
            strategy_md,
            "# ma_crossover@1.0.0 Research Evidence\n\n"
            "State: **research only**. Promotion is not authorized.\n\n"
            f"Code hash: `{strategy['code_hash']}`  \nParameter hash: `{strategy['parameter_set_hash']}`\n\n"
            f"Deterministic replay: {strategy['deterministic_replay']['passed']} over {strategy['deterministic_replay']['input_bars']} mock/synthetic bars. "
            "Next-bar execution is implemented; survivorship bias remains unresolved. Transaction-cost, slippage, parameter, regime, concentration and drawdown analyses are in the JSON evidence.\n\n"
            "The review is non-independent because operator-reviewer is both operator and proposed reviewer. Synthetic results do not guarantee profit and cannot qualify a campaign.\n",
        ),
    }
    result["risk_markdown"] = {
        "path": str(risk_md),
        "sha256": _write_text(
            risk_md,
            "# Risk-Limit Approval Pack\n\nAll limits are **unapproved and inactive**. The JSON table records proposed value, unit, rationale, effect, conservative alternative, failure behavior, evidence requirement, and a blocking scenario for every limit.\n\nNo limit in this pack authorizes financial advice, campaign creation, proposals, orders, or fills.\n",
        ),
    }
    result["approval_markdown"] = {
        "path": str(approval_md),
        "sha256": _write_text(
            approval_md,
            "# Updated Pre-Campaign Approval Pack\n\n"
            "ma_crossover@1.0.0 is registered as **research only**. Rules and fees remain assumed, unapproved, inactive and unreferenced. GP, ACI and BRACBANK are approved only for data collection, validation, research and backtesting. BDT 1,000,000 is simulated capital only.\n\n"
            "operator-reviewer is the proposed reviewer and also the operator, so review is explicitly non-independent. No risk limits are approved. The campaign remains uncreated and blocked at 0/60 with no sessions, proposals, orders or fills.\n\n"
            "Separate future approvals are required for independent risk review, risk limits, each strategy promotion, authoritative rule/fee verification and activation, campaign symbols and campaign creation. No profit is guaranteed.\n",
        ),
    }
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
