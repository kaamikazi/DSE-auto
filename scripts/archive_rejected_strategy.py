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
from app.services.five_symbol_robustness import assert_registry_identity  # noqa: E402
from app.services.historical_strategy_research import sha256_file  # noqa: E402
from app.services.report_provenance import build_report_provenance  # noqa: E402
from app.services.strategy_research_archival import (  # noqa: E402
    PROPOSED_STRATEGY,
    REJECTION_REASONS,
    archive_registration_evidence,
    archived_benchmark_contract,
    assert_archived_state,
    bounded_experiment_matrix,
    canonical_hash,
    data_requirement_report,
    new_strategy_specification,
)
from scripts.run_five_symbol_robustness import (  # noqa: E402
    CODE_HASH,
    EXTENSION_ID,
    PARAMETER_HASH,
    PARENT_ID,
    REGISTRATION_ID,
    expected_identity,
    identity_snapshot,
)

AUTHORIZED_HEAD = "6d910080947cccc90fd8547d0cad5d04647773bd"
SOURCE_MANIFEST_HASH = (
    "f4202023e17cf72e478368e79329e83aa6a0c3644861a5f4649f76d0635e876f"
)
SOURCE_RESULT_HASH = "b9627c390f299d2f48f11a812a682d0e4c69fc30531c61e71b696f1a1f988f81"
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


def cross_sectional_registration_count(db: Any) -> int:
    return int(
        db.scalar(
            select(func.count())
            .select_from(StrategyRegistration)
            .where(StrategyRegistration.strategy_id == "cross_sectional_momentum")
        )
        or 0
    )


def load_source_result() -> tuple[dict[str, Any], Path, dict[str, Any]]:
    root = ROOT / "reports" / "strategy_research"
    for manifest_path in sorted(root.glob("risk-control-attribution-*/manifest.json")):
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("manifest_hash") != SOURCE_MANIFEST_HASH:
            continue
        mismatches = []
        for item in manifest["files"]:
            path = manifest_path.parent / item["name"]
            if not path.is_file() or sha256_file(path) != item["sha256"]:
                mismatches.append(item["name"])
        if mismatches:
            raise RuntimeError(f"Source evidence hash mismatch: {mismatches}")
        result_path = manifest_path.with_name("research_result.json")
        if sha256_file(result_path) != SOURCE_RESULT_HASH:
            raise RuntimeError("Pinned risk-control result hash mismatch")
        result = json.loads(result_path.read_text(encoding="utf-8"))
        if result["decision"]["research_role"] != "reject_strategy":
            raise RuntimeError("Pinned result does not authorize rejection")
        return result, result_path, manifest
    raise RuntimeError("Pinned risk-control attribution evidence is unavailable")


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


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def closure_markdown(payload: dict[str, Any]) -> str:
    result = payload["source_research_result"]
    matrix = payload["bounded_experiment_matrix"]
    requirements = payload["data_requirements"]
    reasons = "\n".join(
        f"- `{item['code']}` — {item['reason']}"
        for item in payload["rejection_reasons"]
    )
    return "\n".join(
        [
            "# ma_crossover@1.0.0 research closure",
            "",
            "## Technical conclusion",
            "",
            "`ma_crossover@1.0.0` is formally rejected as a research hypothesis and preserved as an immutable comparison benchmark. Its implementation passed technical validation; the evidence did not establish a robust edge. Lifecycle remains `research`, promotion and campaign eligibility remain blocked, and execution and real-money authorization are false.",
            "",
            "## Rejection evidence",
            "",
            f"The five-symbol strategy returned {result['exact_reproduction']['strategy_net_return_percent']:.2f}% versus {result['exact_reproduction']['buy_hold_net_return_percent']:.2f}% for equal-weight buy-and-hold. BRACBANK supplied {result['return_attribution']['bracbank']['percent_of_profit']:.2f}% of profit; eight chronological partitions were negative; deterministic trade labels counted 57 whipsaws and 48 profitable trend captures.",
            "",
            reasons,
            "",
            "## The benchmark identity is frozen",
            "",
            "The registration ID, code hash, parameter hash, dataset identities, timing contract, cost assumptions, baseline results, leave-one-out results, walk-forward results, and final decision are preserved. Future work may compare against this benchmark but may not promote it without an entirely new independent review and explicit governance authorization.",
            "",
            "## A different family is designed, not implemented",
            "",
            f"`{PROPOSED_STRATEGY}` is a design-only cross-sectional relative-momentum proposal. The bounded matrix contains {matrix['configuration_count']} configurations formed before results from four lookbacks, two rebalance frequencies, three selection rules, and three weighting rules. No configuration was selected or evaluated.",
            "",
            "## Universe quality blocks research execution",
            "",
            f"At least {requirements['minimum_research_approved_symbols']} research-approved symbols across at least {requirements['minimum_sectors']} sectors are required; 15–25 are preferred. The current five symbols are sufficient only for an engineering dry run, not a research conclusion. Observed data bounds remain non-official lifecycle evidence.",
            "",
            "## Governance and limitations",
            "",
            "The new family remains unregistered and unimplemented with zero qualification contribution. Implementation, registration, and execution require three separate later authorizations. DSEX is unavailable and is not substituted. Liquidity filters are prohibited until volume units and semantics are verified. No strategy, campaign, session, signal, proposal, order, transaction, fill, or broker workflow was executed.",
            "",
            "## Recommended next step",
            "",
            requirements["next_data_milestone"] + ".",
        ]
    )


def artifact(payload: dict[str, Any], generated: str) -> dict[str, Any]:
    result = payload["source_research_result"]
    baselines = [
        {
            "baseline": row["baseline"],
            "return_percent": round(float(row["total_return_percent"]), 4),
            "drawdown_percent": round(float(row["maximum_drawdown_percent"]), 4),
        }
        for row in result["baseline_comparison"]
    ]
    matrix_dimensions = [
        {"dimension": "momentum measurements", "candidate_count": 4},
        {"dimension": "rebalance frequencies", "candidate_count": 2},
        {"dimension": "portfolio selections", "candidate_count": 3},
        {"dimension": "weighting methods", "candidate_count": 3},
    ]
    body = closure_markdown(payload)
    sources = [
        {
            "id": "archive",
            "label": "Archived strategy decision",
            "path": "archived_strategy_decision.json",
        },
        {
            "id": "benchmark",
            "label": "Immutable archived benchmark contract",
            "path": "archived_benchmark_manifest.json",
        },
        {
            "id": "matrix",
            "label": "Predeclared bounded experiment matrix",
            "path": "bounded_experiment_matrix.json",
        },
    ]
    return {
        "manifest": {
            "surface": "report",
            "version": 1,
            "title": "Closing 20/50 research and bounding the next strategy family",
            "generatedAt": generated,
            "sources": sources,
            "blocks": [
                {"id": "summary", "type": "markdown", "body": body},
                {
                    "id": "baseline_note",
                    "type": "markdown",
                    "body": "## Simpler benchmarks frame the rejection\n\nNet return alone does not determine preference, but the archived result trails both equal-weight buy-and-hold and monthly rebalancing while remaining concentrated in BRACBANK.",
                    "sourceId": "benchmark",
                },
                {"id": "baseline_chart", "type": "chart", "chartId": "baselines"},
                {
                    "id": "matrix_note",
                    "type": "markdown",
                    "body": "## The next experiment is bounded before observation\n\nThe four design axes create exactly 72 configurations. None is selected, ranked, optimized, implemented, registered, or executed.",
                    "sourceId": "matrix",
                },
                {"id": "matrix_chart", "type": "chart", "chartId": "matrix"},
            ],
            "charts": [
                {
                    "id": "baselines",
                    "type": "bar",
                    "title": "Net return across archived predeclared baselines",
                    "dataset": "baselines",
                    "sourceId": "benchmark",
                    "valueFormat": ".2f",
                    "encodings": {
                        "x": {
                            "field": "baseline",
                            "type": "nominal",
                            "label": "Baseline",
                        },
                        "y": {
                            "field": "return_percent",
                            "type": "quantitative",
                            "label": "Net return (%)",
                            "format": ".2f",
                        },
                    },
                },
                {
                    "id": "matrix",
                    "type": "bar",
                    "title": "Predeclared candidates per experiment dimension",
                    "dataset": "matrix_dimensions",
                    "sourceId": "matrix",
                    "valueFormat": ".0f",
                    "encodings": {
                        "x": {
                            "field": "dimension",
                            "type": "nominal",
                            "label": "Dimension",
                        },
                        "y": {
                            "field": "candidate_count",
                            "type": "quantitative",
                            "label": "Candidate count",
                            "format": ".0f",
                        },
                    },
                },
            ],
        },
        "snapshot": {
            "datasets": {
                "baselines": baselines,
                "matrix_dimensions": matrix_dimensions,
            }
        },
    }


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
        "new_strategy_registrations_before": prior["new_strategy_registrations_before"],
        "new_strategy_registrations_after": prior["new_strategy_registrations_after"],
        "archived_identity_before": prior["archived_identity_before"],
        "archived_identity_after": prior["archived_identity_after"],
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
    ) != (
        "paper",
        False,
        "disabled",
    ):
        raise RuntimeError("Paper-only safety mismatch")
    if git_head() != args.expected_head:
        raise RuntimeError(f"Execution HEAD mismatch: {git_head()}")
    if subprocess.run(
        ["git", "merge-base", "--is-ancestor", AUTHORIZED_HEAD, git_head()], cwd=ROOT
    ).returncode:
        raise RuntimeError("Authorized HEAD is not an ancestor of execution HEAD")

    authorization = args.authorization_file.read_text(encoding="utf-8")
    authorization_sha256 = hashlib.sha256(authorization.encode()).hexdigest()
    source_result, source_result_path, source_manifest = load_source_result()
    matrix = bounded_experiment_matrix()
    new_spec = new_strategy_specification()
    requirements = data_requirement_report(5)
    generated = datetime.now(UTC).isoformat()
    events: list[dict[str, str]] = []

    with SessionLocal() as db:
        if not verify_audit_chain(db):
            raise RuntimeError("Canonical audit invalid")
        registration = db.get(StrategyRegistration, REGISTRATION_ID)
        parent = db.get(ResearchDataset, PARENT_ID)
        extension = db.get(ResearchDataset, EXTENSION_ID)
        if not registration or not parent or not extension:
            raise RuntimeError("Pinned strategy or dataset identity missing")
        identity = identity_snapshot(registration, parent, extension)
        assert_registry_identity(identity, expected_identity())
        if (
            registration.code_hash != CODE_HASH
            or canonical_hash(registration.parameters) != PARAMETER_HASH
        ):
            raise RuntimeError("Strategy code or parameter hash mismatch")
        if registration.lifecycle_state != "research":
            raise RuntimeError("Strategy lifecycle is not research")
        if (
            registration.evidence.get("promotion_status") != "blocked"
            or registration.evidence.get("campaign_eligibility") is not False
        ):
            raise RuntimeError("Pre-archive governance is not fail closed")
        before = protected_counts(db)
        new_before = cross_sectional_registration_count(db)
        if new_before:
            raise RuntimeError("Proposed strategy is already registered")
        identity_before = {
            "registration_id": registration.id,
            "code_hash": registration.code_hash,
            "parameter_hash": canonical_hash(registration.parameters),
            "dataset_ids": [parent.id, extension.id],
            "dataset_hashes": [parent.dataset_hash, extension.dataset_hash],
        }
        provenance = build_report_provenance(
            db,
            database_role=settings.DATABASE_ROLE,
            environment=settings.APP_ENV,
            database_url=settings.DATABASE_URL,
            dataset_ids=[PARENT_ID, EXTENSION_ID],
            strategy_version="ma_crossover@1.0.0 archived research benchmark",
        )

    contract = archived_benchmark_contract(
        identity,
        source_result,
        result_sha256=SOURCE_RESULT_HASH,
        source_manifest_sha256=SOURCE_MANIFEST_HASH,
    )
    decision = {
        "strategy": "ma_crossover@1.0.0",
        "registration_id": REGISTRATION_ID,
        "decision": "rejected",
        "research_role": "archived_rejected_benchmark",
        "lifecycle": "research",
        "technical_validation": "passed",
        "research_hypothesis": "failed_to_establish_robust_edge",
        "rejection_reasons": list(REJECTION_REASONS),
        "promotion_authorized": False,
        "campaign_eligibility": False,
        "execution_authorization": False,
        "real_money_eligibility": False,
        "qualification": "0/60",
        "source_result_sha256": SOURCE_RESULT_HASH,
        "source_manifest_hash": SOURCE_MANIFEST_HASH,
        "decided_at": generated,
    }
    decision_sha256 = canonical_hash(decision)
    payload = {
        "identity": identity,
        "provenance": provenance,
        "authorization_sha256": authorization_sha256,
        "archived_strategy_decision": decision,
        "archived_strategy_decision_sha256": decision_sha256,
        "archived_benchmark_contract": contract,
        "rejection_reasons": list(REJECTION_REASONS),
        "source_research_result": source_result,
        "source_research_result_path": str(source_result_path.relative_to(ROOT)),
        "source_research_manifest": source_manifest,
        "new_strategy_specification": new_spec,
        "bounded_experiment_matrix": matrix,
        "data_requirements": requirements,
        "no_implementation": True,
        "no_execution": True,
        "no_trading_operations": True,
        "qualification": "0/60",
    }

    run_id = f"strategy-closure-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{authorization_sha256[:8]}"
    output = ROOT / "reports" / "strategy_research" / run_id
    output.mkdir(parents=True)
    (output / "archived_strategy_decision.json").write_text(
        json.dumps(decision, indent=2, sort_keys=True), encoding="utf-8"
    )
    (output / "archived_benchmark_manifest.json").write_text(
        json.dumps(contract, indent=2, sort_keys=True), encoding="utf-8"
    )
    (output / "cross_sectional_momentum_spec.json").write_text(
        json.dumps(new_spec, indent=2, sort_keys=True), encoding="utf-8"
    )
    (output / "bounded_experiment_matrix.json").write_text(
        json.dumps(matrix, indent=2, sort_keys=True), encoding="utf-8"
    )
    write_csv(output / "bounded_experiment_matrix.csv", matrix["rows"])
    (output / "data_requirement_report.json").write_text(
        json.dumps(requirements, indent=2, sort_keys=True), encoding="utf-8"
    )
    (output / "closure_report.md").write_text(
        closure_markdown(payload), encoding="utf-8"
    )
    (output / "artifact.json").write_text(
        json.dumps(artifact(payload, generated), indent=2, sort_keys=True),
        encoding="utf-8",
    )

    with SessionLocal() as db:
        registration = db.get(StrategyRegistration, REGISTRATION_ID)
        if not registration:
            raise RuntimeError("Strategy registration disappeared")
        archive_registration_evidence(
            registration,
            contract,
            decision_sha256=decision_sha256,
            authorization_sha256=authorization_sha256,
        )
        db.add(registration)
        db.commit()
        for event_type, state in (
            (
                "strategy.research_rejected",
                {
                    "decision_sha256": decision_sha256,
                    "reasons": list(REJECTION_REASONS),
                },
            ),
            (
                "strategy.archived_benchmark_designated",
                {"contract_sha256": canonical_hash(contract), "immutable": True},
            ),
            (
                "strategy.promotion_prohibited",
                {"promotion_status": "blocked", "promotion_authorized": False},
            ),
            ("strategy.campaign_prohibited", {"campaign_eligibility": False}),
            (
                "strategy.new_family_research_proposed",
                {
                    "strategy": PROPOSED_STRATEGY,
                    "spec_sha256": canonical_hash(new_spec),
                },
            ),
            (
                "strategy.new_family_unregistered",
                {"strategy": PROPOSED_STRATEGY, "registration": "absent"},
            ),
            (
                "strategy.new_family_execution_unauthorized",
                {"strategy": PROPOSED_STRATEGY, "execution_authorization": False},
            ),
        ):
            append_event(db, events, event_type, state, args.operator)
        registration = db.get(StrategyRegistration, REGISTRATION_ID)
        if not registration:
            raise RuntimeError("Strategy registration disappeared after archival")
        assert_archived_state(registration)
        after = protected_counts(db)
        new_after = cross_sectional_registration_count(db)
        identity_after = {
            "registration_id": registration.id,
            "code_hash": registration.code_hash,
            "parameter_hash": canonical_hash(registration.parameters),
            "dataset_ids": [PARENT_ID, EXTENSION_ID],
            "dataset_hashes": [identity["parent_hash"], identity["extension_hash"]],
        }
        if before != after or new_before != new_after or new_after != 0:
            raise RuntimeError("Operational state or proposed registration changed")
        if identity_before != identity_after:
            raise RuntimeError("Archived benchmark identity changed")
        if not verify_audit_chain(db):
            raise RuntimeError("Canonical audit invalid after archival")
        canonical_status = audit_status(db)

    audit_record = {
        "authorization_text": authorization,
        "authorization_sha256": authorization_sha256,
        "events": events,
        "event_count": len(events),
        "canonical_status": canonical_status,
    }
    (output / "audit_record.json").write_text(
        json.dumps(audit_record, indent=2, sort_keys=True), encoding="utf-8"
    )
    provisional = {
        "files": [],
        "html_status": "pending_builder_validation",
        "protected_counts_before": before,
        "protected_counts_after": after,
        "new_strategy_registrations_before": new_before,
        "new_strategy_registrations_after": new_after,
        "archived_identity_before": identity_before,
        "archived_identity_after": identity_after,
    }
    provisional["manifest_hash"] = canonical_hash(provisional)
    (output / "manifest.json").write_text(
        json.dumps(provisional, indent=2, sort_keys=True), encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "output": str(output),
                "archived_status": "archived_rejected_benchmark",
                "new_strategy": "design_only_unregistered",
                "configuration_count": matrix["configuration_count"],
                "audit_events": len(events),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
