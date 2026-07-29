from __future__ import annotations

import argparse
import csv
import hashlib
import json
import subprocess
import sys
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, func, select, text

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
from app.services.five_symbol_robustness import (  # noqa: E402
    EXTENSION_SYMBOLS,
    FIVE_SYMBOLS,
    PARENT_SYMBOLS,
    assert_registry_identity,
    combine_weighted,
    concentration_summary,
    cost_stress,
    deterministic_best,
    leave_one_out,
    parameter_universe_stability,
    run_portfolio,
    run_portfolio_buy_hold,
    sector_weights,
    symbol_summaries,
    validate_combined_datasets,
)
from app.services.historical_strategy_research import (  # noqa: E402
    REGISTERED_PARAMETERS,
    corporate_action_analysis,
    sha256_file,
    timing_semantics,
    walk_forward_analysis,
)
from app.services.report_provenance import build_report_provenance  # noqa: E402

AUTHORIZED_HEAD = "db438947bfe72597d20c751c75570eb2467301d6"
REGISTRATION_ID = "4faf2623-f458-4d96-93d0-e70e8af8f7f6"
PARENT_ID = "ba5f2d99-6c66-4e37-ae31-d48c8ee47b15"
PARENT_NAME = "gp-aci-bracbank-research-f24a48cb729e8a65"
PARENT_HASH = "ddfeee6bbf5324f9f64fd604e9d4bfc7bd2a45ce2896e7b968037af58de04791"
EXTENSION_ID = "c6da44f7-b842-4d0a-a8e0-31bad7f96bea"
EXTENSION_NAME = "batbc-squrpharma-t2-extension-5357a454f66e1ea7"
EXTENSION_HASH = "4470633c8d62c627357e6c0a6472466142e7a6539f7100d9de677827dda8c882"
CODE_HASH = "b3b8e3bbce398d084b1b971332876861745e40f11600d83e9435e4c5e4ecb3b3"
PARAMETER_HASH = "51d34977e7e67cb3045ec624e7e0f6474fb24390f6427fa1d0f307e4ee7df13e"
PARENT_APPROVAL_PACK_HASH = (
    "28f44bd78a9e57e0e00ecf56046f5411e18f48508585ef4cbae0ad3e52207235"
)
PROTECTED = (ValidationCampaign, PaperSession, Signal, Order, Transaction)


def canonical_hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def git_head() -> str:
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
    ).strip()


def protected_counts(db: Any) -> dict[str, int]:
    return {
        model.__tablename__: int(
            db.scalar(select(func.count()).select_from(model)) or 0
        )
        for model in PROTECTED
    }


def identity_snapshot(
    registration: StrategyRegistration,
    parent: ResearchDataset,
    extension: ResearchDataset,
) -> dict[str, Any]:
    return {
        "registration_id": registration.id,
        "strategy": f"{registration.strategy_id}@{registration.version}",
        "lifecycle": registration.lifecycle_state,
        "code_hash": registration.code_hash,
        "parameter_hash": canonical_hash(registration.parameters),
        "parameters": registration.parameters,
        "parent_registry_id": parent.id,
        "parent_version": parent.name,
        "parent_hash": parent.dataset_hash,
        "parent_symbols": parent.symbols,
        "parent_status": parent.status,
        "extension_registry_id": extension.id,
        "extension_version": extension.name,
        "extension_hash": extension.dataset_hash,
        "extension_symbols": extension.symbols,
        "extension_status": extension.status,
        "promotion_status": registration.evidence.get("promotion_status"),
        "campaign_eligibility": registration.evidence.get("campaign_eligibility"),
    }


def expected_identity() -> dict[str, Any]:
    return {
        "registration_id": REGISTRATION_ID,
        "strategy": "ma_crossover@1.0.0",
        "lifecycle": "research",
        "code_hash": CODE_HASH,
        "parameter_hash": PARAMETER_HASH,
        "parameters": REGISTERED_PARAMETERS,
        "parent_registry_id": PARENT_ID,
        "parent_version": PARENT_NAME,
        "parent_hash": PARENT_HASH,
        "parent_symbols": list(PARENT_SYMBOLS),
        "parent_status": "research_dataset_active",
        "extension_registry_id": EXTENSION_ID,
        "extension_version": EXTENSION_NAME,
        "extension_hash": EXTENSION_HASH,
        "extension_symbols": list(EXTENSION_SYMBOLS),
        "extension_status": "research_dataset_active",
        "promotion_status": "blocked",
        "campaign_eligibility": False,
    }


def source_exclusions() -> tuple[dict[str, list[str]], dict[str, Any]]:
    for candidate in sorted(
        (ROOT / "reports" / "target_subset_approval").glob("*/provisional_subset.json"),
        reverse=True,
    ):
        manifest = json.loads(
            candidate.with_name("manifest.json").read_text(encoding="utf-8")
        )
        if manifest.get("manifest_hash") != PARENT_APPROVAL_PACK_HASH:
            continue
        parent = json.loads(candidate.read_text(encoding="utf-8"))
        break
    else:
        raise RuntimeError("Preserved parent exclusion ledger is unavailable")
    dates: dict[str, list[str]] = {symbol: [] for symbol in FIVE_SYMBOLS}
    for row in parent["ledger"]:
        if (
            row["status"] == "held_for_corporate_action"
            and row["adjustment_status"] == "adjusted"
        ):
            dates[str(row["symbol"])].append(str(row["date"]))
    extension_path = (
        ROOT
        / "reports"
        / "pilot_research_extension"
        / EXTENSION_NAME
        / "activation_result.json"
    )
    extension = json.loads(extension_path.read_text(encoding="utf-8"))
    return dates, {
        "parent": {
            "ledger": str(candidate.relative_to(ROOT)),
            "status_counts": parent["candidate_status_counts"],
            "corporate_action_adjusted_dates_by_symbol": {
                symbol: sorted(set(values))
                for symbol, values in dates.items()
                if values
            },
        },
        "extension": {
            "evidence": str(extension_path.relative_to(ROOT)),
            "excluded_by_symbol_and_disposition": extension["summary"][
                "excluded_by_symbol_and_disposition"
            ],
            "note": "T3 alternatives, invalid/duplicate records, and lifecycle holds are not synthesized as missing sessions or corporate actions.",
        },
    }


def append_event(
    db: Any,
    events: list[dict[str, str]],
    event_type: str,
    state: dict[str, Any],
    operator: str,
) -> None:
    event = append_audit(
        db,
        actor=operator,
        event_type=event_type,
        entity_type="strategy_registration",
        entity_id=REGISTRATION_ID,
        new_state=state,
    )
    events.append({"id": event.id, "type": event_type, "hash": event.integrity_hash})


def result_metrics(run: dict[str, Any]) -> dict[str, Any]:
    return {"net": run["net"]["metrics"], "gross": run["gross"]["metrics"]}


def clean_walk_forward(walk: dict[str, Any]) -> dict[str, Any]:
    partition_returns = [
        float(part["out_of_sample_metrics"].get("total_return_percent") or 0)
        for row in walk["symbols"].values()
        for part in row["partitions"]
    ]
    holdout_returns = [
        float(row["final_holdout"]["metrics"].get("total_return_percent") or 0)
        for row in walk["symbols"].values()
    ]
    walk["dispersion"] = {
        "partition_min_return_percent": min(partition_returns),
        "partition_max_return_percent": max(partition_returns),
        "positive_partitions": sum(v > 0 for v in partition_returns),
        "negative_partitions": sum(v < 0 for v in partition_returns),
        "holdout_min_return_percent": min(holdout_returns),
        "holdout_max_return_percent": max(holdout_returns),
    }
    return walk


def verdict(payload: dict[str, Any]) -> dict[str, Any]:
    full = float(payload["baseline"]["equal_weight"]["net"]["total_return_percent"])
    no_brac = float(payload["leave_bracbank_out"]["net"]["total_return_percent"])
    no_best = float(payload["leave_best_out"]["net"]["total_return_percent"])
    benchmark = float(
        payload["benchmarks"]["equal_weight"]["net"]["total_return_percent"]
    )
    holdout = float(payload["walk_forward"]["combined_holdout_return_percent"])
    nearby = float(payload["parameter_stability"]["nearby_positive_share"])
    worst_cost = min(
        float(row["universes"]["full"]["total_return_percent"])
        for row in payload["cost_stress"]["scenarios"]
    )
    reasons = []
    if full > 0 and (no_brac <= 0 or no_best <= 0):
        reasons.append(
            "performance collapses after removing BRACBANK or the deterministic best symbol"
        )
    if benchmark > full and abs(
        float(payload["benchmarks"]["equal_weight"]["net"]["maximum_drawdown_percent"])
    ) <= abs(
        float(payload["baseline"]["equal_weight"]["net"]["maximum_drawdown_percent"])
    ):
        reasons.append(
            "buy-and-hold is stronger without demonstrated drawdown compensation"
        )
    if holdout <= 0 or not payload["walk_forward"]["all_symbol_holdouts_positive"]:
        reasons.append("walk-forward holdouts are inconsistent")
    if nearby < 2 / 3:
        reasons.append("nearby parameter results are unstable")
    if worst_cost <= 0:
        reasons.append("bounded cost stress erases the apparent edge")
    reasons.extend(
        [
            "both datasets are concentrated in one underlying third-party source",
            "BATBC and SQURPHARMA lifecycle evidence remains pending",
            "corporate-action evidence and DSEX benchmark remain unavailable",
        ]
    )
    label = "insufficient_evidence" if reasons else "research_promising"
    return {
        "verdict": label,
        "fail_closed_reasons": reasons,
        "promotion_authorized": False,
        "qualification": "0/60",
    }


def markdown_report(payload: dict[str, Any]) -> str:
    lines = [
        "# Five-symbol ma_crossover robustness study",
        "",
        "> Research-only evidence. No profit guarantee or real-money authorization.",
        "",
        f"Verdict: **{payload['verdict']['verdict']}**",
        "",
        "## Technical summary",
        "",
        "The registered 20/50 strategy was evaluated without changing timing, parameters, or safety state. The conclusion is fail-closed and cannot promote the strategy.",
        "",
        "## Net baseline",
        "",
        "| Scope | Return % | Annualized % | Drawdown % | Sharpe | Trades |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for symbol, summary in payload["baseline"]["symbols"].items():
        m = summary["net"]
        lines.append(
            f"| {symbol} | {m['total_return_percent']:.4f} | {m['annualized_return_percent']:.4f} | {m['maximum_drawdown_percent']:.4f} | {m.get('sharpe_ratio')} | {m['number_of_trades']} |"
        )
    m = payload["baseline"]["equal_weight"]["net"]
    lines.append(
        f"| Equal weight | {m['total_return_percent']:.4f} | {m['annualized_return_percent']:.4f} | {m['maximum_drawdown_percent']:.4f} | {m.get('sharpe_ratio')} | {m['number_of_trades']} |"
    )
    lines += [
        "",
        "## Robustness findings",
        "",
        f"BRACBANK removal return: {payload['leave_bracbank_out']['net']['total_return_percent']:.4f}%. Deterministic best symbol removed: {payload['leave_best_out']['removed_symbol']}; resulting return: {payload['leave_best_out']['net']['total_return_percent']:.4f}%.",
        "",
        "## Methodology",
        "",
        "Signals use source-present closes and execute no earlier than the next source-present open. Missing sessions are not synthesized. Gross means zero modeled fee and slippage; net uses the declared conservative draft costs.",
        "",
        "## Limitations and missing evidence",
        "",
    ]
    lines += [f"- {reason}" for reason in payload["verdict"]["fail_closed_reasons"]]
    lines += [
        "",
        "DSEX was unavailable and not substituted. Lifecycle dates are not claimed as official. Qualification remains 0/60.",
        "",
        "## Next steps",
        "",
        "Independent source and lifecycle evidence, verified corporate actions, authoritative costs, and independent review are required before any governance reconsideration.",
    ]
    return "\n".join(lines) + "\n"


def artifact(payload: dict[str, Any], generated: str) -> dict[str, Any]:
    rows = [
        {
            "scope": symbol,
            "net_return_percent": round(
                float(summary["net"]["total_return_percent"]), 4
            ),
            "drawdown_percent": round(
                float(summary["net"]["maximum_drawdown_percent"]), 4
            ),
        }
        for symbol, summary in payload["baseline"]["symbols"].items()
    ]
    rows.append(
        {
            "scope": "Equal weight",
            "net_return_percent": round(
                float(
                    payload["baseline"]["equal_weight"]["net"]["total_return_percent"]
                ),
                4,
            ),
            "drawdown_percent": round(
                float(
                    payload["baseline"]["equal_weight"]["net"][
                        "maximum_drawdown_percent"
                    ]
                ),
                4,
            ),
        }
    )
    chart_sql = (
        "SELECT scope, net_return_percent, drawdown_percent "
        "FROM baseline ORDER BY sort_order"
    )
    chart_engine = create_engine("sqlite://")
    with chart_engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE baseline (sort_order INTEGER, scope TEXT, net_return_percent REAL, drawdown_percent REAL)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO baseline VALUES (:sort_order, :scope, :net_return_percent, :drawdown_percent)"
            ),
            [{"sort_order": index, **row} for index, row in enumerate(rows)],
        )
        rows = [dict(row._mapping) for row in connection.execute(text(chart_sql))]
    chart_engine.dispose()
    source = {
        "id": "research_result",
        "label": "Five-symbol robustness result",
        "path": "research_result.json",
        "query": {
            "sql": chart_sql,
            "description": "Projection of the Python-produced baseline summary through an in-memory SQLite table; this query does not perform the backtest.",
            "engine": "sqlite",
            "language": "sql",
            "executed_at": generated,
        },
    }
    narrative = markdown_report(payload)
    return {
        "surface": "report",
        "manifest": {
            "version": 1,
            "surface": "report",
            "title": "Five-symbol ma_crossover robustness study",
            "generatedAt": generated,
            "charts": [
                {
                    "id": "baseline_returns",
                    "title": "Net baseline return by scope",
                    "type": "bar",
                    "dataset": "baseline",
                    "sourceId": "research_result",
                    "encodings": {
                        "x": {"field": "scope", "type": "nominal", "label": "Scope"},
                        "y": {
                            "field": "net_return_percent",
                            "type": "quantitative",
                            "label": "Net return (%)",
                            "format": ".2f",
                        },
                    },
                    "valueFormat": ".2f",
                }
            ],
            "sources": [source],
            "blocks": [
                {"id": "narrative", "type": "markdown", "body": narrative},
                {"id": "returns", "type": "chart", "chartId": "baseline_returns"},
            ],
        },
        "snapshot": {
            "version": 1,
            "generatedAt": generated,
            "status": "ready",
            "datasets": {"baseline": rows},
        },
    }


def repair_evidence(output: Path) -> None:
    payload_path = output / "research_result.json"
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    parent_path = ROOT / "data" / "research_datasets" / f"{PARENT_NAME}.jsonl"
    extension_path = ROOT / "data" / "research_datasets" / f"{EXTENSION_NAME}.jsonl"
    bars, validation = validate_combined_datasets(parent_path, extension_path)
    excluded_dates, exclusions = source_exclusions()
    full = run_portfolio(bars)
    excluded = {symbol: len(excluded_dates[symbol]) for symbol in FIVE_SYMBOLS}
    summaries = symbol_summaries(full, bars, excluded)
    best = deterministic_best(summaries)
    weights = sector_weights(list(FIVE_SYMBOLS))
    sector = combine_weighted(full["net_results"], weights, bars)
    sector_gross = combine_weighted(full["gross_results"], weights, bars)
    buy_hold = run_portfolio_buy_hold(bars)
    buy_sector = combine_weighted(buy_hold["net_results"], weights, bars)
    buy_sector_gross = combine_weighted(buy_hold["gross_results"], weights, bars)
    no_brac_bars = {
        symbol: rows for symbol, rows in bars.items() if symbol != "BRACBANK"
    }
    no_best_bars = {symbol: rows for symbol, rows in bars.items() if symbol != best}
    no_brac, no_best = run_portfolio(no_brac_bars), run_portfolio(no_best_bars)
    payload["baseline"] = {
        "symbols": summaries,
        "equal_weight": result_metrics(full),
        "sector_balanced": {
            "net": sector["metrics"],
            "gross": sector_gross["metrics"],
            "weights": weights,
        },
    }
    payload["benchmarks"] = {
        "symbols_net": {
            symbol: dict(result.metrics)
            for symbol, result in buy_hold["net_results"].items()
        },
        "equal_weight": result_metrics(buy_hold),
        "sector_balanced": {
            "net": buy_sector["metrics"],
            "gross": buy_sector_gross["metrics"],
            "weights": weights,
        },
        "cash": {"return_percent": 0.0},
        "dsex": "unavailable_not_substituted",
    }
    payload["leave_bracbank_out"] = {
        **result_metrics(no_brac),
        "benchmark": result_metrics(run_portfolio_buy_hold(no_brac_bars)),
        "change_vs_full_percent_points": float(
            no_brac["net"]["metrics"]["total_return_percent"]
        )
        - float(full["net"]["metrics"]["total_return_percent"]),
    }
    payload["leave_best_out"] = {
        "removed_symbol": best,
        "predefined_rule": "highest completed baseline net total return; alphabetical tie break",
        **result_metrics(no_best),
        "benchmark": result_metrics(run_portfolio_buy_hold(no_best_bars)),
    }
    payload["leave_one_out"] = leave_one_out(bars)
    payload["concentration"] = concentration_summary(
        float(full["net"]["metrics"]["total_return_percent"]), payload["leave_one_out"]
    )
    payload["parameter_stability"] = parameter_universe_stability(bars, best)
    payload["cost_stress"] = cost_stress(bars, best)
    payload["data_quality_sensitivity"]["dataset_origin_results"] = {
        "parent_only": result_metrics(
            run_portfolio({symbol: bars[symbol] for symbol in PARENT_SYMBOLS})
        ),
        "extension_only": result_metrics(
            run_portfolio({symbol: bars[symbol] for symbol in EXTENSION_SYMBOLS})
        ),
        "combined": result_metrics(full),
    }
    payload["corporate_action_sensitivity"] = corporate_action_analysis(
        bars, excluded_dates, full["net_results"]
    )
    payload["source_exclusions"] = exclusions
    payload["pre_run_validation"]["source_exclusions"] = exclusions
    payload["pre_run_validation"]["mandatory_passed"] = validation["mandatory_passed"]
    payload["baseline"]["equal_weight"]["net"]["missing_data_exclusions"] = sum(
        excluded.values()
    )
    payload["baseline"]["equal_weight"]["gross"]["missing_data_exclusions"] = sum(
        excluded.values()
    )
    payload["verdict"] = verdict(payload)
    payload_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8"
    )
    (output / "pre_run_validation.json").write_text(
        json.dumps(payload["pre_run_validation"], indent=2, sort_keys=True),
        encoding="utf-8",
    )
    (output / "report.md").write_text(markdown_report(payload), encoding="utf-8")
    generated = str(
        json.loads((output / "artifact.json").read_text(encoding="utf-8"))["manifest"][
            "generatedAt"
        ]
    )
    (output / "artifact.json").write_text(
        json.dumps(artifact(payload, generated), indent=2, sort_keys=True),
        encoding="utf-8",
    )


def finalize_evidence(output: Path, html_status: str) -> None:
    manifest_path = output / "manifest.json"
    prior = json.loads(manifest_path.read_text(encoding="utf-8"))
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
        "html_status": html_status,
        "protected_counts_before": prior["protected_counts_before"],
        "protected_counts_after": prior["protected_counts_after"],
        "strategy_state_before": prior["strategy_state_before"],
        "strategy_state_after": prior["strategy_state_after"],
    }
    manifest["manifest_hash"] = canonical_hash(manifest)
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--authorization-file", type=Path)
    parser.add_argument("--operator", default="operator")
    parser.add_argument("--expected-head")
    parser.add_argument("--repair-output", type=Path)
    parser.add_argument("--finalize-output", type=Path)
    parser.add_argument(
        "--html-status", default="builder_structural_only_browser_qa_unavailable"
    )
    args = parser.parse_args()
    if args.repair_output:
        repair_evidence(args.repair_output)
        return 0
    if args.finalize_output:
        finalize_evidence(args.finalize_output, args.html_status)
        return 0
    if args.authorization_file is None or args.expected_head is None:
        parser.error(
            "--authorization-file and --expected-head are required for execution"
        )
    settings = get_settings()
    if (
        settings.TRADING_MODE,
        settings.LIVE_TRADING_ENABLED,
        settings.BROKER_ADAPTER,
    ) != ("paper", False, "disabled"):
        raise RuntimeError("Paper-only safety mismatch")
    if git_head() != args.expected_head:
        raise RuntimeError(f"Execution HEAD mismatch: {git_head()}")
    if (
        not subprocess.run(
            ["git", "merge-base", "--is-ancestor", AUTHORIZED_HEAD, git_head()],
            cwd=ROOT,
        ).returncode
        == 0
    ):
        raise RuntimeError("Authorized HEAD is not an ancestor")
    authorization = args.authorization_file.read_text(encoding="utf-8")
    authorization_hash = hashlib.sha256(authorization.encode()).hexdigest()
    parent_path = ROOT / "data" / "research_datasets" / f"{PARENT_NAME}.jsonl"
    extension_path = ROOT / "data" / "research_datasets" / f"{EXTENSION_NAME}.jsonl"
    if (
        sha256_file(parent_path) != PARENT_HASH
        or sha256_file(extension_path) != EXTENSION_HASH
    ):
        raise RuntimeError("Dataset file hash mismatch")
    bars, validation = validate_combined_datasets(parent_path, extension_path)
    excluded_dates, exclusions = source_exclusions()
    validation["source_exclusions"] = exclusions
    events: list[dict[str, str]] = []
    with SessionLocal() as db:
        if not verify_audit_chain(db):
            raise RuntimeError("Canonical audit invalid")
        registration, parent, extension = (
            db.get(StrategyRegistration, REGISTRATION_ID),
            db.get(ResearchDataset, PARENT_ID),
            db.get(ResearchDataset, EXTENSION_ID),
        )
        if not registration or not parent or not extension:
            raise RuntimeError("Pinned registry record missing")
        identity = identity_snapshot(registration, parent, extension)
        assert_registry_identity(identity, expected_identity())
        provenance = build_report_provenance(
            db,
            database_role=settings.DATABASE_ROLE,
            environment=settings.APP_ENV,
            database_url=settings.DATABASE_URL,
            dataset_ids=[PARENT_ID, EXTENSION_ID],
            strategy_version="ma_crossover@1.0.0 research",
        )
        identity.update(
            {
                "authorized_head": AUTHORIZED_HEAD,
                "execution_head": git_head(),
                "operational_database_fingerprint": provenance["database_fingerprint"],
                "canonical_audit_chain_id": provenance["audit_chain_id"],
            }
        )
        before = protected_counts(db)
        strategy_before = registration.lifecycle_state
        append_event(
            db,
            events,
            "strategy.five_symbol_execution_authorized",
            {"authorization_sha256": authorization_hash, "research_only": True},
            args.operator,
        )
        append_event(
            db,
            events,
            "strategy.five_symbol_identity_verified",
            identity,
            args.operator,
        )
        append_event(
            db, events, "strategy.five_symbol_data_validated", validation, args.operator
        )

    excluded = {symbol: len(excluded_dates[symbol]) for symbol in FIVE_SYMBOLS}
    full = run_portfolio(bars)
    summaries = symbol_summaries(full, bars, excluded)
    best = deterministic_best(summaries)
    sector = combine_weighted(
        full["net_results"], sector_weights(list(FIVE_SYMBOLS)), bars
    )
    sector_gross = combine_weighted(
        full["gross_results"], sector_weights(list(FIVE_SYMBOLS)), bars
    )
    buy_hold = run_portfolio_buy_hold(bars)
    buy_sector = combine_weighted(
        buy_hold["net_results"], sector_weights(list(FIVE_SYMBOLS)), bars
    )
    buy_sector_gross = combine_weighted(
        buy_hold["gross_results"], sector_weights(list(FIVE_SYMBOLS)), bars
    )
    no_brac_bars = {s: rows for s, rows in bars.items() if s != "BRACBANK"}
    no_brac = run_portfolio(no_brac_bars)
    no_brac_bh = run_portfolio_buy_hold(no_brac_bars)
    no_best_bars = {s: rows for s, rows in bars.items() if s != best}
    no_best = run_portfolio(no_best_bars)
    no_best_bh = run_portfolio_buy_hold(no_best_bars)
    loo = leave_one_out(bars)
    walk = clean_walk_forward(walk_forward_analysis(bars))
    params = parameter_universe_stability(bars, best)
    costs = cost_stress(bars, best)
    origin_runs = {
        "parent_only": result_metrics(
            run_portfolio({s: bars[s] for s in PARENT_SYMBOLS})
        ),
        "extension_only": result_metrics(
            run_portfolio({s: bars[s] for s in EXTENSION_SYMBOLS})
        ),
        "combined": result_metrics(full),
    }
    corporate = corporate_action_analysis(bars, excluded_dates, full["net_results"])
    payload: dict[str, Any] = {
        "identity": identity,
        "authorization_sha256": authorization_hash,
        "provenance": provenance,
        "pre_run_validation": validation,
        "timing_contract": timing_semantics(),
        "baseline": {
            "symbols": summaries,
            "equal_weight": result_metrics(full),
            "sector_balanced": {
                "net": sector["metrics"],
                "gross": sector_gross["metrics"],
                "weights": sector["weights"],
            },
        },
        "benchmarks": {
            "symbols_net": {
                s: dict(r.metrics) for s, r in buy_hold["net_results"].items()
            },
            "equal_weight": result_metrics(buy_hold),
            "sector_balanced": {
                "net": buy_sector["metrics"],
                "gross": buy_sector_gross["metrics"],
                "weights": buy_sector["weights"],
            },
            "cash": {"return_percent": 0.0},
            "dsex": "unavailable_not_substituted",
        },
        "leave_bracbank_out": {
            **result_metrics(no_brac),
            "benchmark": result_metrics(no_brac_bh),
            "change_vs_full_percent_points": float(
                no_brac["net"]["metrics"]["total_return_percent"]
            )
            - float(full["net"]["metrics"]["total_return_percent"]),
        },
        "leave_best_out": {
            "removed_symbol": best,
            "predefined_rule": "highest completed baseline net total return; alphabetical tie break",
            **result_metrics(no_best),
            "benchmark": result_metrics(no_best_bh),
        },
        "leave_one_out": loo,
        "concentration": concentration_summary(
            float(full["net"]["metrics"]["total_return_percent"]), loo
        ),
        "walk_forward": walk,
        "parameter_stability": params,
        "cost_stress": costs,
        "data_quality_sensitivity": {
            "dataset_origin_results": origin_runs,
            "single_source_concentration": "both registered datasets derive primarily from the same Mendeley adjusted/unadjusted source",
            "extension_evidence_strength": "tier_2_single_source_high_quality",
            "lifecycle_evidence": "pending; observed research windows are not official listing dates",
        },
        "corporate_action_sensitivity": corporate,
        "source_exclusions": exclusions,
        "missing_evidence": [
            "DSEX benchmark",
            "official lifecycle dates",
            "verified corporate actions",
            "authoritative fees",
            "independent source confirmation",
            "independent strategy review",
        ],
        "no_profit_guarantee": True,
        "no_real_money_authorization": True,
        "qualification": "0/60",
    }
    payload["verdict"] = verdict(payload)
    run_id = f"five-symbol-robustness-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{authorization_hash[:8]}"
    output = ROOT / "reports" / "strategy_research" / run_id
    output.mkdir(parents=True)
    generated = datetime.now(UTC).isoformat()
    (output / "research_result.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8"
    )
    (output / "pre_run_validation.json").write_text(
        json.dumps(validation, indent=2, sort_keys=True), encoding="utf-8"
    )
    (output / "report.md").write_text(markdown_report(payload), encoding="utf-8")
    (output / "artifact.json").write_text(
        json.dumps(artifact(payload, generated), indent=2, sort_keys=True),
        encoding="utf-8",
    )
    with (output / "trade_ledger.csv").open(
        "w", newline="", encoding="utf-8"
    ) as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "experiment",
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
        ledgers = {
            "baseline": full["net_results"],
            "leave_bracbank_out": no_brac["net_results"],
            "leave_best_out": no_best["net_results"],
        }
        for experiment, results in ledgers.items():
            for symbol, result in results.items():
                for trade in result.trades:
                    writer.writerow(
                        {"experiment": experiment, "symbol": symbol, **asdict(trade)}
                    )

    with SessionLocal() as db:
        for event_type, state in (
            (
                "strategy.five_symbol_baseline_completed",
                {"metrics": payload["baseline"], "operational_effect": False},
            ),
            (
                "strategy.five_symbol_leave_bracbank_completed",
                payload["leave_bracbank_out"],
            ),
            ("strategy.five_symbol_leave_best_completed", payload["leave_best_out"]),
            ("strategy.five_symbol_leave_one_out_completed", payload["concentration"]),
            (
                "strategy.five_symbol_walk_forward_completed",
                payload["walk_forward"]["dispersion"],
            ),
            ("strategy.five_symbol_evidence_generated", {"run_id": run_id}),
            ("strategy.five_symbol_verdict_recorded", payload["verdict"]),
            (
                "strategy.promotion_prohibited",
                {
                    "promotion_status": "blocked",
                    "campaign_eligibility": False,
                    "qualification": "0/60",
                },
            ),
        ):
            append_event(db, events, event_type, state, args.operator)
        registration = db.get(StrategyRegistration, REGISTRATION_ID)
        after = protected_counts(db)
        if (
            before != after
            or not registration
            or registration.lifecycle_state != strategy_before
            or not verify_audit_chain(db)
        ):
            raise RuntimeError("Protected state or audit changed")
        post_audit = audit_status(db)
    audit_record = {
        "events": events,
        "event_count": len(events),
        "canonical_status": post_audit,
        "authorization_text": authorization,
        "authorization_sha256": authorization_hash,
    }
    (output / "audit_record.json").write_text(
        json.dumps(audit_record, indent=2, sort_keys=True), encoding="utf-8"
    )
    manifest = {
        "files": [
            {
                "name": path.name,
                "bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            }
            for path in sorted(output.iterdir())
            if path.is_file() and path.name not in {"manifest.json", "report.html"}
        ],
        "protected_counts_before": before,
        "protected_counts_after": after,
        "strategy_state_before": strategy_before,
        "strategy_state_after": strategy_before,
    }
    manifest["manifest_hash"] = canonical_hash(manifest)
    (output / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "output": str(output),
                "verdict": payload["verdict"],
                "events": len(events),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
