from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import subprocess
import sys
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import func, select

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.core.config import get_settings  # noqa: E402
from app.core.database import SessionLocal  # noqa: E402
from app.models import (  # noqa: E402
    Order,
    PaperSession,
    ResearchDataset,
    Signal,
    StrategyRegistration,
    Transaction,
    ValidationCampaign,
)
from app.services.audit import append_audit, audit_status, verify_audit_chain  # noqa: E402
from app.services.historical_strategy_research import (  # noqa: E402
    ALLOWED_SYMBOLS,
    REGISTERED_PARAMETERS,
    baseline_analysis,
    benchmark_analysis,
    corporate_action_analysis,
    cost_sensitivity,
    parameter_sensitivity,
    research_verdict,
    robustness_analysis,
    sha256_file,
    tier_sensitivity,
    timing_semantics,
    validate_and_load_dataset,
    walk_forward_analysis,
)
from app.services.report_provenance import build_report_provenance  # noqa: E402
from app.services.research_strategy_registration import (  # noqa: E402
    APPROVED_CODE_HASH,
    APPROVED_DATASET_HASH,
    APPROVED_DATASET_ID,
    APPROVED_DATASET_NAME,
    APPROVED_PARAMETER_HASH,
    verify_strategy_artifacts,
)

EXPECTED_HEAD = "488cf33284de276917b0b1188f6b10571215568b"
REGISTRATION_ID = "4faf2623-f458-4d96-93d0-e70e8af8f7f6"
APPROVAL_PACK_HASH = "28f44bd78a9e57e0e00ecf56046f5411e18f48508585ef4cbae0ad3e52207235"
PROTECTED = (ValidationCampaign, PaperSession, Signal, Order, Transaction)


def _canonical_hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _head() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _counts(db: Any) -> dict[str, int]:
    return {
        model.__tablename__: int(
            db.scalar(select(func.count()).select_from(model)) or 0
        )
        for model in PROTECTED
    }


def assert_pinned_identity(identity: dict[str, Any], artifacts: dict[str, Any]) -> None:
    expected = {
        "registration_id": REGISTRATION_ID,
        "strategy": "ma_crossover@1.0.0",
        "lifecycle": "research",
        "code_hash": APPROVED_CODE_HASH,
        "parameter_hash": APPROVED_PARAMETER_HASH,
        "parameters": REGISTERED_PARAMETERS,
        "dataset_registry_id": APPROVED_DATASET_ID,
        "dataset_id": APPROVED_DATASET_NAME,
        "dataset_hash": APPROVED_DATASET_HASH,
        "promotion_status": "blocked",
        "campaign_eligibility": False,
        "git_head": EXPECTED_HEAD,
    }
    if (
        identity != expected
        or artifacts.get("code_hash") != APPROVED_CODE_HASH
        or artifacts.get("parameter_hash") != APPROVED_PARAMETER_HASH
    ):
        raise RuntimeError(f"Pinned identity mismatch: {identity}")


def _write_json(path: Path, payload: object) -> None:
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8"
    )


def _clean_baseline(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "summaries": value["summaries"],
        "combined": {
            key: item
            for key, item in value["combined"].items()
            if key != "equity_curve"
        },
    }


def _markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# ma_crossover@1.0.0 historical research result",
        "",
        "> Research evidence only. No profit guarantee, promotion, campaign, paper session,",
        "> proposal, order, fill, broker access, or real-money authorization.",
        "",
        f"Final verdict: **{payload['verdict']['verdict']}**",
        "",
        "## Baseline net results",
        "",
        "| Scope | Return % | Annualized % | Max drawdown % | Trades |",
        "|---|---:|---:|---:|---:|",
    ]
    for symbol in ALLOWED_SYMBOLS:
        metrics = payload["baseline"]["summaries"][symbol]["net"]
        lines.append(
            f"| {symbol} | {metrics['total_return_percent']:.4f} | "
            f"{metrics['annualized_return_percent']:.4f} | "
            f"{metrics['maximum_drawdown_percent']:.4f} | {metrics['number_of_trades']} |"
        )
    combined = payload["baseline"]["combined"]["metrics"]
    lines.append(
        f"| Equal weight | {combined['total_return_percent']:.4f} | "
        f"{combined['annualized_return_percent']:.4f} | "
        f"{combined['maximum_drawdown_percent']:.4f} | {combined['number_of_trades']} |"
    )
    lines.extend(
        [
            "",
            "## Method and limitations",
            "",
            "Signals use adjusted closes after bar completion and execute no earlier than the next source-present open. "
            "The adjusted series is an approved historical research view, not proof of point-in-time corporate-action knowledge. "
            "DSEX is unavailable and was not substituted. Tier 2 is not treated as cross-source confirmed.",
            "",
            "## Fail-closed reasons",
            "",
            *[f"- {reason}" for reason in payload["verdict"]["fail_closed_reasons"]],
            "",
            "Qualification remains 0/60.",
        ]
    )
    return "\n".join(lines) + "\n"


def _html(markdown_text: str, payload: dict[str, Any]) -> str:
    rows = []
    for symbol in ALLOWED_SYMBOLS:
        metric = payload["baseline"]["summaries"][symbol]["net"]
        rows.append(
            f"<tr><td>{symbol}</td><td>{metric['total_return_percent']:.4f}%</td>"
            f"<td>{metric['maximum_drawdown_percent']:.4f}%</td><td>{metric['number_of_trades']}</td></tr>"
        )
    return (
        "<!doctype html><html><head><meta charset='utf-8'><title>Historical research evidence</title>"
        "<style>body{font:16px system-ui;max-width:1100px;margin:40px auto;padding:0 20px;color:#18212f}"
        "table{border-collapse:collapse;width:100%}th,td{padding:8px;border:1px solid #ccd3dc;text-align:right}"
        "th:first-child,td:first-child{text-align:left}.warning{background:#fff4ce;padding:14px}</style></head><body>"
        "<h1>ma_crossover@1.0.0 historical research</h1><p class='warning'>Research evidence only; no profit guarantee or real-money authorization.</p>"
        f"<h2>Verdict: {html.escape(payload['verdict']['verdict'])}</h2>"
        "<table><thead><tr><th>Symbol</th><th>Net return</th><th>Max drawdown</th><th>Trade events</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table><h2>Canonical narrative</h2><pre>{html.escape(markdown_text)}</pre>"
        "<p>Self-contained semantic HTML; browser visual QA was not performed.</p></body></html>"
    )


def _excluded_ledger() -> tuple[dict[str, int], dict[str, list[str]], Path]:
    candidates = sorted(
        (ROOT / "reports" / "target_subset_approval").glob("*/provisional_subset.json")
    )
    for path in reversed(candidates):
        manifest_path = path.with_name("manifest.json")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("manifest_hash") == APPROVAL_PACK_HASH:
            payload = json.loads(path.read_text(encoding="utf-8"))
            break
    else:
        raise RuntimeError("Approved provisional exclusion ledger not found")
    counts = {
        str(key): int(value)
        for key, value in payload["candidate_status_counts"].items()
    }
    dates: dict[str, list[str]] = {symbol: [] for symbol in ALLOWED_SYMBOLS}
    for row in payload["ledger"]:
        if (
            row["status"] == "held_for_corporate_action"
            and row["adjustment_status"] == "adjusted"
        ):
            dates[str(row["symbol"])].append(str(row["date"]))
    return counts, dates, path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--authorization-file", type=Path, required=True)
    parser.add_argument("--operator", default="operator")
    args = parser.parse_args()
    settings = get_settings()
    if (
        settings.TRADING_MODE,
        settings.LIVE_TRADING_ENABLED,
        settings.BROKER_ADAPTER,
    ) != ("paper", False, "disabled"):
        raise RuntimeError("Paper-only safety state mismatch")
    if _head() != EXPECTED_HEAD:
        raise RuntimeError(f"Git HEAD mismatch: {_head()}")
    authorization = args.authorization_file.read_text(encoding="utf-8")
    authorization_hash = hashlib.sha256(authorization.encode()).hexdigest()
    dataset_path = (
        ROOT / "data" / "research_datasets" / f"{APPROVED_DATASET_NAME}.jsonl"
    )
    artifacts = verify_strategy_artifacts(ROOT)
    if sha256_file(dataset_path) != APPROVED_DATASET_HASH:
        raise RuntimeError("Dataset file hash mismatch")
    bars, validation = validate_and_load_dataset(dataset_path)
    status_counts, excluded_dates, exclusion_ledger = _excluded_ledger()
    required_exclusions = {
        "approvable_after_human_decision": 18103,
        "held_for_calendar": 68,
        "held_for_conflict": 8,
        "held_for_corporate_action": 710,
        "held_for_mapping": 240,
        "rejected_invalid": 712,
    }
    if status_counts != required_exclusions:
        raise RuntimeError("Exclusion ledger counts mismatch")
    validation["held_or_rejected_rows_in_active_file"] = 0
    validation["source_exclusion_counts"] = status_counts
    validation["source_exclusion_ledger"] = str(exclusion_ledger)
    validation["mandatory_passed"] = bool(
        validation["mandatory_passed"] and status_counts == required_exclusions
    )

    with SessionLocal() as db:
        if not verify_audit_chain(db):
            raise RuntimeError("Canonical audit invalid before execution")
        registration = db.get(StrategyRegistration, REGISTRATION_ID)
        dataset = db.get(ResearchDataset, APPROVED_DATASET_ID)
        if registration is None or dataset is None:
            raise RuntimeError("Pinned registration or dataset is missing")
        identity = {
            "registration_id": registration.id,
            "strategy": f"{registration.strategy_id}@{registration.version}",
            "lifecycle": registration.lifecycle_state,
            "code_hash": registration.code_hash,
            "parameter_hash": _canonical_hash(registration.parameters),
            "parameters": registration.parameters,
            "dataset_registry_id": dataset.id,
            "dataset_id": dataset.name,
            "dataset_hash": dataset.dataset_hash,
            "promotion_status": registration.evidence.get("promotion_status"),
            "campaign_eligibility": registration.evidence.get("campaign_eligibility"),
            "git_head": _head(),
        }
        assert_pinned_identity(identity, artifacts)
        provenance = build_report_provenance(
            db,
            database_role=settings.DATABASE_ROLE,
            environment=settings.APP_ENV,
            database_url=settings.DATABASE_URL,
            dataset_ids=[dataset.id],
            strategy_version="ma_crossover@1.0.0 research",
        )
        identity["database_fingerprint"] = provenance["database_fingerprint"]
        identity["audit_chain_id"] = provenance["audit_chain_id"]
        before = _counts(db)
        strategy_state = registration.lifecycle_state
        events = []
        for event_type, state in (
            (
                "strategy.research_execution_authorized",
                {
                    "authorization_sha256": authorization_hash,
                    "scope": "research_execution_only",
                },
            ),
            ("strategy.research_identity_verified", identity),
            ("strategy.research_pre_run_validated", validation),
        ):
            event = append_audit(
                db,
                actor=args.operator,
                event_type=event_type,
                entity_type="strategy_registration",
                entity_id=REGISTRATION_ID,
                new_state=state,
            )
            events.append(
                {"id": event.id, "type": event_type, "hash": event.integrity_hash}
            )

    baseline_raw = baseline_analysis(
        bars, {symbol: len(excluded_dates[symbol]) for symbol in ALLOWED_SYMBOLS}
    )
    payload: dict[str, Any] = {
        "identity": identity,
        "authorization_sha256": authorization_hash,
        "pre_run_validation": validation,
        "timing_semantics": timing_semantics(),
        "baseline": _clean_baseline(baseline_raw),
        "benchmarks": benchmark_analysis(bars),
        "walk_forward": walk_forward_analysis(bars),
        "sensitivity": parameter_sensitivity(bars),
        "cost_sensitivity": cost_sensitivity(bars),
        "corporate_actions": corporate_action_analysis(
            bars, excluded_dates, baseline_raw["results"]
        ),
        "tier_sensitivity": tier_sensitivity(bars, dataset_path),
        "provenance": provenance,
        "missing_evidence": [
            "DSEX benchmark",
            "verified corporate actions",
            "authoritative fees",
            "independent strategy review",
        ],
        "no_profit_guarantee": True,
        "no_real_money_authorization": True,
        "qualification": "0/60",
    }
    payload["robustness"] = robustness_analysis(bars, baseline_raw["results"])
    payload["verdict"] = research_verdict(payload)
    run_id = f"ma-crossover-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{authorization_hash[:8]}"
    output = ROOT / "reports" / "strategy_research" / run_id
    output.mkdir(parents=True)
    _write_json(output / "research_result.json", payload)
    _write_json(output / "pre_run_validation.json", validation)
    _write_json(
        output / "artifact.json",
        {
            "schema_version": "1.0",
            "title": "ma_crossover historical research",
            "classification": "research_only",
            "source": "research_result.json",
            "verdict": payload["verdict"],
        },
    )
    with (output / "trade_ledger.csv").open(
        "w", newline="", encoding="utf-8"
    ) as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "symbol",
                "timestamp",
                "side",
                "quantity",
                "price",
                "fee",
                "slippage",
            ],
        )
        writer.writeheader()
        for symbol, result in baseline_raw["results"].items():
            for trade in result.trades:
                writer.writerow({"symbol": symbol, **asdict(trade)})
    md = _markdown(payload)
    (output / "report.md").write_text(md, encoding="utf-8")
    (output / "report.html").write_text(_html(md, payload), encoding="utf-8")

    with SessionLocal() as db:
        for event_type, state in (
            (
                "strategy.research_execution_completed",
                {
                    "run_id": run_id,
                    "symbols": list(ALLOWED_SYMBOLS),
                    "operational_trading_entities_created": False,
                },
            ),
            (
                "strategy.research_evidence_generated",
                {
                    "run_id": run_id,
                    "output_hashes": {
                        p.name: sha256_file(p)
                        for p in sorted(output.iterdir())
                        if p.is_file()
                    },
                },
            ),
            ("strategy.research_verdict_recorded", payload["verdict"]),
            (
                "strategy.promotion_prohibited",
                {
                    "promotion_status": "blocked",
                    "campaign_eligibility": False,
                    "qualification": "0/60",
                },
            ),
        ):
            event = append_audit(
                db,
                actor=args.operator,
                event_type=event_type,
                entity_type="strategy_registration",
                entity_id=REGISTRATION_ID,
                new_state=state,
            )
            events.append(
                {"id": event.id, "type": event_type, "hash": event.integrity_hash}
            )
        registration = db.get(StrategyRegistration, REGISTRATION_ID)
        after = _counts(db)
        if (
            before != after
            or registration is None
            or registration.lifecycle_state != strategy_state
            or not verify_audit_chain(db)
        ):
            raise RuntimeError("Protected state or canonical audit verification failed")
        post_audit = audit_status(db)

    audit_record = {
        "events": events,
        "canonical_status": post_audit,
        "authorization_text": authorization,
        "authorization_sha256": authorization_hash,
    }
    _write_json(output / "audit_record.json", audit_record)
    manifest = {
        "files": [
            {
                "name": path.name,
                "bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            }
            for path in sorted(output.iterdir())
            if path.is_file() and path.name != "manifest.json"
        ],
        "html_status": "self_contained_semantic_html_browser_qa_not_performed",
        "protected_counts_before": before,
        "protected_counts_after": after,
        "strategy_state_before": strategy_state,
        "strategy_state_after": strategy_state,
    }
    manifest["manifest_hash"] = _canonical_hash(manifest)
    _write_json(output / "manifest.json", manifest)
    print(
        json.dumps(
            {
                "output": str(output),
                "verdict": payload["verdict"],
                "manifest": manifest,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
