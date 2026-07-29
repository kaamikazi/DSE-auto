from __future__ import annotations

import argparse
import csv
import hashlib
import json
import subprocess
import sys
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
    FIVE_SYMBOLS,
    assert_registry_identity,
    run_portfolio,
    run_portfolio_buy_hold,
    symbol_summaries,
    validate_combined_datasets,
)
from app.services.historical_strategy_research import sha256_file  # noqa: E402
from app.services.report_provenance import build_report_provenance  # noqa: E402
from app.services.risk_control_attribution import (  # noqa: E402
    classify_regimes,
    closed_trade_records,
    cost_benefit,
    drawdown_attribution,
    exposure_matched_benchmark,
    regime_analysis,
    research_decision,
    return_attribution,
    simple_baselines,
    symbol_dependence,
    trade_failure_summary,
    walk_forward_failure_analysis,
)
from scripts.run_five_symbol_robustness import (  # noqa: E402
    CODE_HASH,
    EXTENSION_HASH,
    EXTENSION_ID,
    EXTENSION_NAME,
    PARAMETER_HASH,
    PARENT_HASH,
    PARENT_ID,
    PARENT_NAME,
    REGISTRATION_ID,
    canonical_hash,
    expected_identity,
    identity_snapshot,
    source_exclusions,
)

AUTHORIZED_HEAD = "e1b110370e50a0bf49807365c735d3f0de54924d"
PRIOR_MANIFEST_HASH = "baec9a2d756ccd4b419087662b830207eb97a103d4f47d575bb5e20b960700a2"
PRIOR_RESULT_HASH = "e878b981b8e036f1a8259d243b2beb9b13e5324194936b384e0509b4d86001bc"
PROTECTED = (ValidationCampaign, PaperSession, Signal, Order, Transaction)


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


def load_prior_result() -> tuple[dict[str, Any], Path]:
    for manifest_path in sorted(
        (ROOT / "reports" / "strategy_research").glob(
            "five-symbol-robustness-*/manifest.json"
        ),
        reverse=True,
    ):
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("manifest_hash") != PRIOR_MANIFEST_HASH:
            continue
        result_path = manifest_path.with_name("research_result.json")
        if sha256_file(result_path) != PRIOR_RESULT_HASH:
            raise RuntimeError("Prior verified result hash mismatch")
        return json.loads(result_path.read_text(encoding="utf-8")), result_path
    raise RuntimeError("Pinned prior five-symbol result is unavailable")


def metric_view(value: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value.get(key)
        for key in (
            "total_return_percent",
            "annualized_return_percent",
            "maximum_drawdown_percent",
            "drawdown_duration_days",
            "volatility_percent",
            "sharpe_ratio",
            "sortino_ratio",
            "calmar_ratio",
            "number_of_trades",
            "completed_trades",
            "win_rate",
            "profit_factor",
            "expectancy_bdt",
            "average_holding_period_days",
            "exposure_percent",
            "turnover_rate",
            "fee_impact_bdt",
            "slippage_impact_bdt",
        )
    }


def baseline_rows(baselines: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {"baseline": name, **metric_view(value["metrics"])}
        for name, value in baselines.items()
    ]


def markdown_report(payload: dict[str, Any]) -> str:
    role = payload["decision"]["research_role"]
    ma = payload["baselines"]["registered_20_50_crossover"]["metrics"]
    bh = payload["baselines"]["equal_weight_buy_and_hold"]["metrics"]
    matched = payload["exposure_matched"]["metrics"]
    brac = payload["return_attribution"]["bracbank"]
    lines = [
        "# Is the 20/50 crossover an independently useful risk control?",
        "",
        "## Technical summary",
        "",
        f"**Research role: `{role}`.** The crossover returned {ma['total_return_percent']:.2f}% with a {ma['maximum_drawdown_percent']:.2f}% maximum drawdown, versus {bh['total_return_percent']:.2f}% and {bh['maximum_drawdown_percent']:.2f}% for equal-weight buy-and-hold. This is descriptive research evidence, not a profit guarantee or real-money authorization.",
        "",
        f"BRACBANK supplied {brac['percent_of_profit']:.2f}% of strategy profit. The fixed exposure-matched benchmark returned {matched['total_return_percent']:.2f}% with a {matched['maximum_drawdown_percent']:.2f}% drawdown. Chronological validation still contains {payload['walk_forward_failures']['negative_validation_partitions']} negative partitions.",
        "",
        "## The apparent diversification is dominated by one symbol",
        "",
        "| Symbol | Return contribution (pp) | Profit share | Trade events | Fee share | Exposure |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for symbol, row in payload["return_attribution"]["symbols"].items():
        lines.append(
            f"| {symbol} | {row['return_contribution_percentage_points']:.2f} | {row['return_contribution_percent_of_profit']:.2f}% | {row['trade_events']} | {row['fee_share_percent']:.2f}% | {row['average_exposure_percent']:.2f}% |"
        )
    lines += [
        "",
        "## Lower drawdown must be separated from lower exposure",
        "",
        f"The crossover avoided {payload['cost_benefit']['versus_buy_and_hold']['drawdown_avoided_percent_points']:.2f} drawdown points while sacrificing {payload['cost_benefit']['versus_buy_and_hold']['return_sacrificed_percent_points']:.2f} return points. Major drawdowns and symbol-level loss contributions are recorded in the JSON evidence.",
        "",
        "## Predeclared simple baselines",
        "",
        "| Baseline | Return | Drawdown | Volatility | Sharpe | Exposure | Turnover |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for row in payload["baseline_comparison"]:
        lines.append(
            f"| {row['baseline']} | {row['total_return_percent']:.2f}% | {row['maximum_drawdown_percent']:.2f}% | {row['volatility_percent']:.2f}% | {row.get('sharpe_ratio')} | {row['exposure_percent']:.2f}% | {row['turnover_rate']:.2f} |"
        )
    lines += [
        "",
        "## Regime and trade failures are conditional diagnostics",
        "",
        "Regimes use only information available before each measured return. Trade labels are deterministic and non-exclusive except for the primary classification. Neither analysis establishes causality.",
        "",
        "## Chronological failures remain unresolved",
        "",
        f"Validation partitions: {payload['walk_forward_failures']['positive_validation_partitions']} positive, {payload['walk_forward_failures']['negative_validation_partitions']} negative, and {payload['walk_forward_failures']['zero_validation_partitions']} flat. Failed holdouts were not retuned.",
        "",
        "## Decision and next step",
        "",
        f"Recommended path: **{payload['decision']['next_research_path']}**. No strategy promotion, activation, campaign, session, proposal, signal, order, transaction, fill, or broker connection was authorized or created. Qualification remains 0/60.",
        "",
        "## Limitations and further questions",
        "",
        "- Both datasets remain concentrated in one underlying third-party source.",
        "- DSEX, authoritative costs, official lifecycle dates, and verified corporate actions remain unavailable.",
        "- Exposure matching is a descriptive decomposition, not an investable recommendation.",
        "- The analysis does not infer investor preferences from risk/return statistics.",
    ]
    return "\n".join(lines) + "\n"


def artifact(payload: dict[str, Any], generated: str) -> dict[str, Any]:
    contribution = [
        {
            "symbol": symbol,
            "return_contribution_percentage_points": round(
                float(row["return_contribution_percentage_points"]), 4
            ),
        }
        for symbol, row in payload["return_attribution"]["symbols"].items()
    ]
    comparisons = [
        {
            "baseline": row["baseline"],
            "return_percent": round(float(row["total_return_percent"]), 4),
            "drawdown_percent": round(float(row["maximum_drawdown_percent"]), 4),
        }
        for row in payload["baseline_comparison"]
    ]
    regimes = [
        {
            "regime": name,
            "relative_return_percent": round(float(row["relative_return_percent"]), 4),
        }
        for name, row in payload["regime_analysis"]["results"].items()
    ]
    engine = create_engine("sqlite://")
    queries = {
        "contribution": "SELECT symbol, return_contribution_percentage_points FROM contribution ORDER BY return_contribution_percentage_points DESC",
        "comparisons": "SELECT baseline, return_percent, drawdown_percent FROM comparisons ORDER BY return_percent DESC",
        "regimes": "SELECT regime, relative_return_percent FROM regimes ORDER BY regime",
    }
    datasets: dict[str, list[dict[str, Any]]] = {}
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE contribution(symbol TEXT, return_contribution_percentage_points REAL)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO contribution VALUES (:symbol,:return_contribution_percentage_points)"
            ),
            contribution,
        )
        connection.execute(
            text(
                "CREATE TABLE comparisons(baseline TEXT, return_percent REAL, drawdown_percent REAL)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO comparisons VALUES (:baseline,:return_percent,:drawdown_percent)"
            ),
            comparisons,
        )
        connection.execute(
            text("CREATE TABLE regimes(regime TEXT, relative_return_percent REAL)")
        )
        connection.execute(
            text("INSERT INTO regimes VALUES (:regime,:relative_return_percent)"),
            regimes,
        )
        for name, query in queries.items():
            datasets[name] = [
                dict(row._mapping) for row in connection.execute(text(query))
            ]
    engine.dispose()
    sources = [
        {
            "id": name,
            "label": f"Deterministic {name} projection",
            "path": "research_result.json",
            "query": {
                "sql": query,
                "description": "Projection of Python-produced research results through an in-memory SQLite table; this query does not run the backtest.",
                "engine": "sqlite",
                "language": "sql",
                "executed_at": generated,
            },
        }
        for name, query in queries.items()
    ]
    narrative = markdown_report(payload)
    blocks = [
        {"id": "summary", "type": "markdown", "body": narrative},
        {
            "id": "contribution_note",
            "type": "markdown",
            "body": "## BRACBANK dominates return attribution\n\nThe chart shows additive percentage-point contribution to the equal-weight strategy return; concentration limits any diversification claim.",
        },
        {"id": "contribution_chart", "type": "chart", "chartId": "contribution"},
        {
            "id": "baseline_note",
            "type": "markdown",
            "body": "## Simple baselines test whether complexity adds value\n\nCompare return and drawdown together; no single metric establishes investor preference.",
        },
        {"id": "baseline_chart", "type": "chart", "chartId": "comparisons"},
        {
            "id": "regime_note",
            "type": "markdown",
            "body": "## Conditional regime results are uneven\n\nRelative return is compounded only over observations bearing each predeclared label; trend and volatility labels overlap.",
        },
        {"id": "regime_chart", "type": "chart", "chartId": "regimes"},
    ]
    charts = [
        {
            "id": "contribution",
            "title": "Return contribution by symbol",
            "type": "bar",
            "dataset": "contribution",
            "sourceId": "contribution",
            "encodings": {
                "x": {"field": "symbol", "type": "nominal", "label": "Symbol"},
                "y": {
                    "field": "return_contribution_percentage_points",
                    "type": "quantitative",
                    "label": "Return contribution (percentage points)",
                    "format": ".2f",
                },
            },
            "valueFormat": ".2f",
        },
        {
            "id": "comparisons",
            "title": "Net return across predeclared baselines",
            "type": "bar",
            "dataset": "comparisons",
            "sourceId": "comparisons",
            "encodings": {
                "x": {"field": "baseline", "type": "nominal", "label": "Baseline"},
                "y": {
                    "field": "return_percent",
                    "type": "quantitative",
                    "label": "Net return (%)",
                    "format": ".2f",
                },
            },
            "valueFormat": ".2f",
        },
        {
            "id": "regimes",
            "title": "Strategy return relative to buy-and-hold by regime",
            "type": "bar",
            "dataset": "regimes",
            "sourceId": "regimes",
            "encodings": {
                "x": {"field": "regime", "type": "nominal", "label": "Regime"},
                "y": {
                    "field": "relative_return_percent",
                    "type": "quantitative",
                    "label": "Relative return (%)",
                    "format": ".2f",
                },
            },
            "valueFormat": ".2f",
        },
    ]
    return {
        "surface": "report",
        "manifest": {
            "version": 1,
            "surface": "report",
            "title": "Is the 20/50 crossover an independently useful risk control?",
            "generatedAt": generated,
            "charts": charts,
            "sources": sources,
            "blocks": blocks,
        },
        "snapshot": {
            "version": 1,
            "generatedAt": generated,
            "status": "ready",
            "datasets": datasets,
        },
    }


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = sorted({field for row in rows for field in row})
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    field: json.dumps(value, sort_keys=True, default=str)
                    if isinstance(value, (dict, list))
                    else value
                    for field, value in row.items()
                }
            )


def finalize_manifest(output: Path, prior: dict[str, Any], html_status: str) -> None:
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
    (output / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--authorization-file", type=Path)
    parser.add_argument("--operator", default="operator")
    parser.add_argument("--expected-head")
    parser.add_argument("--finalize-output", type=Path)
    parser.add_argument(
        "--html-status", default="builder_structural_only_browser_qa_unavailable"
    )
    args = parser.parse_args()
    if args.finalize_output:
        prior = json.loads(
            (args.finalize_output / "manifest.json").read_text(encoding="utf-8")
        )
        finalize_manifest(args.finalize_output, prior, args.html_status)
        return 0
    if args.authorization_file is None or args.expected_head is None:
        parser.error("--authorization-file and --expected-head are required")
    settings = get_settings()
    if (
        settings.TRADING_MODE,
        settings.LIVE_TRADING_ENABLED,
        settings.BROKER_ADAPTER,
    ) != ("paper", False, "disabled"):
        raise RuntimeError("Paper-only safety mismatch")
    if git_head() != args.expected_head:
        raise RuntimeError(f"Execution HEAD mismatch: {git_head()}")
    if subprocess.run(
        ["git", "merge-base", "--is-ancestor", AUTHORIZED_HEAD, git_head()], cwd=ROOT
    ).returncode:
        raise RuntimeError("Authorized HEAD is not an ancestor of execution HEAD")
    authorization = args.authorization_file.read_text(encoding="utf-8")
    authorization_hash = hashlib.sha256(authorization.encode()).hexdigest()
    parent_path = ROOT / "data" / "research_datasets" / f"{PARENT_NAME}.jsonl"
    extension_path = ROOT / "data" / "research_datasets" / f"{EXTENSION_NAME}.jsonl"
    if (
        sha256_file(parent_path) != PARENT_HASH
        or sha256_file(extension_path) != EXTENSION_HASH
    ):
        raise RuntimeError("Dataset hash mismatch")
    bars, validation = validate_combined_datasets(parent_path, extension_path)
    excluded_dates, exclusions = source_exclusions()
    validation["source_exclusions"] = exclusions
    prior_result, prior_path = load_prior_result()
    events: list[dict[str, str]] = []
    with SessionLocal() as db:
        if not verify_audit_chain(db):
            raise RuntimeError("Canonical audit invalid")
        registration = db.get(StrategyRegistration, REGISTRATION_ID)
        parent = db.get(ResearchDataset, PARENT_ID)
        extension = db.get(ResearchDataset, EXTENSION_ID)
        if not registration or not parent or not extension:
            raise RuntimeError("Pinned registry record missing")
        identity = identity_snapshot(registration, parent, extension)
        assert_registry_identity(identity, expected_identity())
        if (
            registration.code_hash != CODE_HASH
            or canonical_hash(registration.parameters) != PARAMETER_HASH
        ):
            raise RuntimeError("Strategy artifact identity mismatch")
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
                "prior_result_path": str(prior_path.relative_to(ROOT)),
                "prior_result_sha256": PRIOR_RESULT_HASH,
            }
        )
        before = protected_counts(db)
        strategy_before = registration.lifecycle_state
        append_event(
            db,
            events,
            "strategy.risk_control_attribution_authorized",
            {"authorization_sha256": authorization_hash, "research_only": True},
            args.operator,
        )
        append_event(
            db,
            events,
            "strategy.risk_control_identity_verified",
            identity,
            args.operator,
        )

    strategy = run_portfolio(bars)
    benchmark = run_portfolio_buy_hold(bars)
    reproduced = {
        "strategy_net_return_percent": round(
            float(strategy["net"]["metrics"]["total_return_percent"]), 2
        ),
        "strategy_annualized_return_percent": round(
            float(strategy["net"]["metrics"]["annualized_return_percent"]), 2
        ),
        "strategy_maximum_drawdown_percent": round(
            float(strategy["net"]["metrics"]["maximum_drawdown_percent"]), 2
        ),
        "strategy_sharpe": round(float(strategy["net"]["metrics"]["sharpe_ratio"]), 3),
        "buy_hold_net_return_percent": round(
            float(benchmark["net"]["metrics"]["total_return_percent"]), 2
        ),
        "buy_hold_maximum_drawdown_percent": round(
            float(benchmark["net"]["metrics"]["maximum_drawdown_percent"]), 2
        ),
    }
    expected = {
        "strategy_net_return_percent": 147.80,
        "strategy_annualized_return_percent": 7.06,
        "strategy_maximum_drawdown_percent": -21.35,
        "strategy_sharpe": 0.651,
        "buy_hold_net_return_percent": 191.49,
        "buy_hold_maximum_drawdown_percent": -52.16,
    }
    if reproduced != expected:
        raise RuntimeError(f"Five-symbol reproduction mismatch: {reproduced}")
    summaries = symbol_summaries(
        strategy, bars, {symbol: len(excluded_dates[symbol]) for symbol in FIVE_SYMBOLS}
    )
    attribution = return_attribution(strategy)
    baselines = simple_baselines(bars, strategy)
    matched = exposure_matched_benchmark(
        bars, float(strategy["net"]["metrics"]["exposure_percent"])
    )
    regimes = classify_regimes(bars)
    regime_results = regime_analysis(bars, strategy, benchmark)
    drawdowns = drawdown_attribution(bars, strategy, benchmark, excluded_dates, regimes)
    trade_records = closed_trade_records(bars, strategy["net_results"], excluded_dates)
    failures = trade_failure_summary(trade_records)
    dependence = symbol_dependence(bars, summaries)
    walk_failures = walk_forward_failure_analysis(
        prior_result["walk_forward"], bars, regimes
    )
    benefit = cost_benefit(strategy, benchmark, matched)
    decision = research_decision(
        dependence, prior_result["walk_forward"], baselines, matched
    )
    payload: dict[str, Any] = {
        "identity": identity,
        "authorization_sha256": authorization_hash,
        "provenance": provenance,
        "pre_run_validation": validation,
        "exact_reproduction": reproduced,
        "return_attribution": attribution,
        "drawdown_attribution": drawdowns,
        "baselines": baselines,
        "baseline_comparison": baseline_rows(baselines),
        "exposure_matched": matched,
        "regime_analysis": regime_results,
        "trade_failure_analysis": failures,
        "symbol_dependence": dependence,
        "walk_forward_failures": walk_failures,
        "cost_benefit": benefit,
        "decision": decision,
        "no_profit_guarantee": True,
        "no_real_money_authorization": True,
        "qualification": "0/60",
    }
    run_id = f"risk-control-attribution-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{authorization_hash[:8]}"
    output = ROOT / "reports" / "strategy_research" / run_id
    output.mkdir(parents=True)
    generated = datetime.now(UTC).isoformat()
    (output / "research_result.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8"
    )
    (output / "report.md").write_text(markdown_report(payload), encoding="utf-8")
    (output / "artifact.json").write_text(
        json.dumps(artifact(payload, generated), indent=2, sort_keys=True),
        encoding="utf-8",
    )
    write_csv(output / "baseline_comparison.csv", payload["baseline_comparison"])
    write_csv(output / "trade_failure_ledger.csv", trade_records)
    write_csv(output / "drawdown_ledger.csv", drawdowns["episodes"])
    write_csv(output / "walk_forward_ledger.csv", walk_failures["rows"])

    with SessionLocal() as db:
        for event_type, state in (
            ("strategy.risk_control_reproduction_verified", reproduced),
            (
                "strategy.risk_control_attribution_completed",
                {
                    "return_attribution": attribution,
                    "drawdown_count": drawdowns["count"],
                },
            ),
            (
                "strategy.risk_control_baselines_completed",
                {
                    "baseline_ids": list(baselines),
                    "exposure_matched": metric_view(matched["metrics"]),
                },
            ),
            (
                "strategy.risk_control_failures_analyzed",
                {
                    "regimes": regime_results["results"],
                    "trade_failure_counts": failures["primary_counts"],
                    "walk_forward": {
                        key: walk_failures[key]
                        for key in (
                            "positive_validation_partitions",
                            "negative_validation_partitions",
                            "zero_validation_partitions",
                        )
                    },
                },
            ),
            ("strategy.risk_control_role_assigned", decision),
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
            or registration.evidence.get("promotion_status") != "blocked"
            or registration.evidence.get("campaign_eligibility") is not False
            or not verify_audit_chain(db)
        ):
            raise RuntimeError("Protected state or audit integrity changed")
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
    provisional = {
        "files": [],
        "html_status": "pending_builder_validation",
        "protected_counts_before": before,
        "protected_counts_after": after,
        "strategy_state_before": strategy_before,
        "strategy_state_after": strategy_before,
    }
    provisional["manifest_hash"] = canonical_hash(provisional)
    (output / "manifest.json").write_text(
        json.dumps(provisional, indent=2, sort_keys=True), encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "output": str(output),
                "decision": decision,
                "reproduction": reproduced,
                "audit_events": len(events),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
