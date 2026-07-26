from __future__ import annotations

import sys

if sys.path and sys.path[0].lower().rstrip("\\/").endswith("scripts"):
    sys.path.pop(0)

import argparse
import csv
import io
import json
import sqlite3
import zipfile
from collections import Counter, defaultdict
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

from sqlalchemy import func, select
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.core.config import get_settings  # noqa: E402
from app.core.database import SessionLocal  # noqa: E402
from app.models import (  # noqa: E402
    DatasetImportRun,
    ExtractedClaim,
    GovernedDataset,
    NormalizedDailyBar,
    Order,
    PaperSession,
    StrategyRegistration,
    Transaction,
    ValidationCampaign,
)
from app.services.audit import append_audit, verify_audit_chain  # noqa: E402
from app.services.canonical_research_candidate import (  # noqa: E402
    INVALID_CATEGORIES,
    TRANSFORMATION_VERSION,
    CanonicalCandidateBuilder,
    DatasetSource,
    calendar_analysis,
    csv_rows,
    mapping_rows,
    review_html,
    sha256_file,
    source_quality_score,
    write_csv,
)
from app.services.authoritative_evidence import canonical_hash  # noqa: E402

ZIP_UNADJUSTED = (
    "Dhaka Stock Exchange End-of-Day Financial Dataset/Full Raw Data/UnAdjusted.csv"
)
ZIP_ADJUSTED = (
    "Dhaka Stock Exchange End-of-Day Financial Dataset/Full Raw Data/Adjusted.csv"
)
UNIVERSE = {"GP", "ACI", "BRACBANK", "DSEX"}


def _assert_safety() -> None:
    settings = get_settings()
    if (
        settings.TRADING_MODE != "paper"
        or settings.LIVE_TRADING_ENABLED
        or settings.BROKER_ADAPTER != "disabled"
    ):
        raise RuntimeError("Canonical candidate generation requires paper-only safety")


def _operational_state(db: Session) -> dict[str, Any]:
    return {
        "campaigns": int(
            db.scalar(select(func.count()).select_from(ValidationCampaign)) or 0
        ),
        "sessions": int(db.scalar(select(func.count()).select_from(PaperSession)) or 0),
        "orders": int(db.scalar(select(func.count()).select_from(Order)) or 0),
        "fills": int(db.scalar(select(func.count()).select_from(Transaction)) or 0),
        "promoted_strategies": int(
            db.scalar(
                select(func.count())
                .select_from(StrategyRegistration)
                .where(
                    StrategyRegistration.lifecycle_state.in_(
                        ["paper_candidate", "paper_active"]
                    )
                )
            )
            or 0
        ),
        "active_research_bars": int(
            db.scalar(
                select(func.count())
                .select_from(NormalizedDailyBar)
                .where(NormalizedDailyBar.active_for_research.is_(True))
            )
            or 0
        ),
        "activated_datasets": int(
            db.scalar(
                select(func.count())
                .select_from(GovernedDataset)
                .where(GovernedDataset.review_status != "registered")
            )
            or 0
        ),
        "non_review_previews": int(
            db.scalar(
                select(func.count())
                .select_from(DatasetImportRun)
                .where(DatasetImportRun.state != "review_required")
            )
            or 0
        ),
        "approved_rule_claims": int(
            db.scalar(
                select(func.count())
                .select_from(ExtractedClaim)
                .where(ExtractedClaim.reviewer_status != "under_review")
            )
            or 0
        ),
    }


def _catalog() -> dict[str, Any]:
    return cast(
        dict[str, Any],
        json.loads(
            (ROOT / "config" / "public_dse_source_catalog.json").read_text(
                encoding="utf-8"
            )
        ),
    )


def _catalog_by_hash() -> dict[str, dict[str, Any]]:
    return {
        str(item["sha256"]): item
        for item in _catalog()["sources"]
        if item["result"] == "downloaded" and item["sha256"]
    }


def _raw_hashes() -> dict[str, str]:
    root = ROOT / "data" / "evidence_workspace" / "public_sources" / "raw"
    result: dict[str, str] = {}
    for item in _catalog()["sources"]:
        if item["result"] != "downloaded":
            continue
        path = root / str(item["local_path"])
        digest = sha256_file(path)
        if digest != item["sha256"] or path.stat().st_size != item["file_size"]:
            raise RuntimeError(f"Preserved raw source changed: {path}")
        result[str(item["local_path"])] = digest
    return result


def _registered_sources(db: Session) -> list[DatasetSource]:
    catalog = _catalog_by_hash()
    sources: list[DatasetSource] = []
    for item in db.scalars(
        select(GovernedDataset).order_by(GovernedDataset.imported_at)
    ):
        metadata = catalog[item.raw_sha256]
        common = {
            "dataset_id": item.id,
            "source_hash": item.raw_sha256,
            "source_path": item.raw_file_path,
            "source_trust": item.source_trust,
            "timestamp_trust": item.timestamp_trust,
            "license_note": item.license_note,
            "stated_row_count": metadata.get("stated_row_count"),
            "stated_symbol_count": metadata.get("stated_symbol_count"),
            "stated_symbol_claim": str(metadata.get("stated_symbol_coverage", "")),
        }
        if Path(item.raw_file_path).suffix.lower() == ".zip":
            sources.extend(
                [
                    DatasetSource(
                        **common,
                        source_name=f"{item.source_name} / unadjusted",
                        adjustment_status="unadjusted",
                        logical_name="Mendeley 23553sm4tn v4 unadjusted",
                    ),
                    DatasetSource(
                        **common,
                        source_name=f"{item.source_name} / adjusted",
                        adjustment_status="adjusted",
                        logical_name="Mendeley 23553sm4tn v4 adjusted",
                    ),
                ]
            )
        else:
            logical_name = item.source_name
            if "5mww8rb9td" not in logical_name and "Historical Data" in logical_name:
                logical_name = "Mendeley 5mww8rb9td v1 historical"
            sources.append(
                DatasetSource(
                    **common,
                    source_name=item.source_name,
                    adjustment_status=item.adjustment_status,
                    logical_name=logical_name,
                )
            )
    return sources


def _ingest(builder: CanonicalCandidateBuilder, source: DatasetSource) -> None:
    path = Path(source.source_path)
    if path.suffix.lower() == ".zip":
        member = (
            ZIP_ADJUSTED if source.adjustment_status == "adjusted" else ZIP_UNADJUSTED
        )
        with zipfile.ZipFile(path) as archive, archive.open(member) as raw_handle:
            with io.TextIOWrapper(
                raw_handle, encoding="utf-8-sig", newline=""
            ) as handle:
                fields, rows = csv_rows(handle, row_prefix=member)
                builder.ingest_rows(source, rows, fields)
    else:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            fields, rows = csv_rows(handle, row_prefix=path.name)
            builder.ingest_rows(source, rows, fields)


def _query_dicts(
    db: sqlite3.Connection, query: str, values: tuple[Any, ...] = ()
) -> list[dict[str, Any]]:
    db.row_factory = sqlite3.Row
    return [dict(row) for row in db.execute(query, values)]


def _export_table(
    db: sqlite3.Connection,
    table: str,
    output: Path,
    *,
    where: str = "",
) -> int:
    db.row_factory = sqlite3.Row
    query = f"SELECT * FROM {table}"  # noqa: S608 - fixed internal table names only
    if where:
        query += f" WHERE {where}"  # noqa: S608 - fixed internal predicate only
    cursor = db.execute(query)
    fields = [item[0] for item in cursor.description or []]
    with output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        count = 0
        for row in cursor:
            writer.writerow(dict(row))
            count += 1
    return count


def _invalid_summary(db: sqlite3.Connection) -> dict[str, Any]:
    rows = _query_dicts(
        db,
        "SELECT source_dataset_id,source_row_id,original_symbol,raw_date,invalid_categories "
        "FROM observations WHERE invalid_categories != '[]' ORDER BY source_dataset_id,source_row_id",
    )
    counts: Counter[str] = Counter()
    examples: dict[str, list[dict[str, Any]]] = {
        name: [] for name in INVALID_CATEGORIES
    }
    for row in rows:
        for category in json.loads(row["invalid_categories"]):
            counts[category] += 1
            if len(examples[category]) < 5:
                examples[category].append(row)
    for row in _query_dicts(db, "SELECT * FROM duplicate_groups"):
        category = row["duplicate_type"]
        normalized = (
            "duplicate_exact"
            if category == "duplicate_exact"
            else "duplicate_conflicting"
        )
        counts[normalized] += int(row["row_count"]) - 1
        if len(examples[normalized]) < 5:
            examples[normalized].append(row)
    action_examples = _query_dicts(
        db,
        "SELECT * FROM corporate_action_candidates ORDER BY normalized_symbol,trading_date LIMIT 5",
    )
    counts["corporate_action_candidate"] = int(
        db.execute("SELECT COUNT(*) FROM corporate_action_candidates").fetchone()[0]
    )
    examples["corporate_action_candidate"] = action_examples
    counts["unresolved"] += int(
        db.execute(
            "SELECT COUNT(*) FROM corporate_action_candidates WHERE candidate_type='unresolved'"
        ).fetchone()[0]
    )
    examples["unresolved"] = _query_dicts(
        db,
        "SELECT * FROM corporate_action_candidates WHERE candidate_type='unresolved' "
        "ORDER BY normalized_symbol,trading_date LIMIT 5",
    )
    return {
        category: {"count": counts[category], "examples": examples[category]}
        for category in INVALID_CATEGORIES
    }


def _calendar_reports(db: sqlite3.Connection) -> list[dict[str, Any]]:
    reports: list[dict[str, Any]] = []
    for (source_name,) in db.execute(
        "SELECT DISTINCT source_name FROM observations ORDER BY source_name"
    ):
        dates = [
            row[0]
            for row in db.execute(
                "SELECT DISTINCT trading_date FROM observations WHERE source_name=? "
                "AND trading_date IS NOT NULL ORDER BY trading_date",
                (source_name,),
            )
        ]
        reports.append({"source_name": source_name, **calendar_analysis(dates)})
    date_sources: defaultdict[str, set[str]] = defaultdict(set)
    for trading_date, source_name in db.execute(
        "SELECT DISTINCT trading_date,source_name FROM observations WHERE trading_date IS NOT NULL"
    ):
        date_sources[trading_date].add(source_name)
    all_sources = {
        row[0] for row in db.execute("SELECT DISTINCT source_name FROM observations")
    }
    disagreements = [
        {
            "trading_date": key,
            "present_sources": sorted(value),
            "missing_sources": sorted(all_sources - value),
        }
        for key, value in sorted(date_sources.items())
        if value != all_sources
    ]
    return reports + [
        {"cross_source_date_disagreements": disagreements, "authoritative": False}
    ]


def _source_quality(
    db: sqlite3.Connection, inventory: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in inventory:
        source_id = item["dataset_id"]
        source_name = item["logical_name"]
        physical_source_name = item["source_name"]
        total = max(int(item["observed_row_count"]), 1)
        invalid = int(
            db.execute(
                "SELECT COUNT(*) FROM observations WHERE source_name=? AND accepted_for_candidate=0",
                (physical_source_name,),
            ).fetchone()[0]
        )
        comparisons, conflicts = db.execute(
            """SELECT COUNT(*),COALESCE(SUM(CASE WHEN final_review_status='unresolved' THEN 1 ELSE 0 END),0)
            FROM cross_source_comparisons WHERE source_name_a=? OR source_name_b=?""",
            (physical_source_name, physical_source_name),
        ).fetchone()
        calendar = calendar_analysis(
            row[0]
            for row in db.execute(
                "SELECT DISTINCT trading_date FROM observations WHERE source_name=? AND trading_date IS NOT NULL",
                (physical_source_name,),
            )
        )
        expected = calendar["date_count"] + len(
            calendar["missing_observed_market_dates"]
        )
        date_rate = calendar["date_count"] / max(expected, 1)
        stated_symbols = item["stated_symbol_count"]
        score = source_quality_score(
            schema_complete=int(item["schema_inconsistencies"]) == 0,
            duplicate_rate=int(item["duplicate_row_count"]) / total,
            invalid_rate=invalid / total,
            conflict_rate=int(conflicts) / max(int(comparisons), 1),
            date_coverage_rate=date_rate,
            symbol_coverage_rate=(
                int(item["unique_symbols"]) / int(stated_symbols)
                if stated_symbols
                else None
            ),
            adjustment_status=str(item["adjustment_status"]),
            license_note=str(item["license_status"]),
            timestamp_trust=str(item["timestamp_trust"]),
            reproducible=True,
            agreement_rate=(1 - int(conflicts) / int(comparisons))
            if comparisons
            else None,
        )
        result.append(
            {
                "logical_name": source_name,
                "dataset_id": source_id,
                **score,
                "rates": {
                    "duplicate": int(item["duplicate_row_count"]) / total,
                    "invalid": invalid / total,
                    "conflict": int(conflicts) / max(int(comparisons), 1),
                    "date_coverage": date_rate,
                },
            }
        )
    return sorted(result, key=lambda row: (-row["score"], row["logical_name"]))


def _universe_readiness(db: sqlite3.Connection) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for symbol in sorted(UNIVERSE):
        aliases = (symbol, "00DSEX") if symbol == "DSEX" else (symbol,)
        placeholders = ",".join("?" for _ in aliases)
        row = db.execute(
            f"""SELECT COUNT(*),MIN(trading_date),MAX(trading_date),
            SUM(CASE WHEN accepted_for_candidate=0 THEN 1 ELSE 0 END)
            FROM observations WHERE normalized_symbol IN ({placeholders}) OR original_symbol IN ({placeholders})""",  # noqa: S608
            (*aliases, *aliases),
        ).fetchone()
        duplicate_count = db.execute(
            f"SELECT COALESCE(SUM(row_count-1),0) FROM duplicate_groups WHERE normalized_symbol IN ({placeholders})",  # noqa: S608
            aliases,
        ).fetchone()[0]
        conflicts = db.execute(
            f"SELECT COUNT(*) FROM cross_source_comparisons WHERE normalized_symbol IN ({placeholders}) AND final_review_status='unresolved'",  # noqa: S608
            aliases,
        ).fetchone()[0]
        actions = db.execute(
            f"SELECT COUNT(*) FROM corporate_action_candidates WHERE normalized_symbol IN ({placeholders})",  # noqa: S608
            aliases,
        ).fetchone()[0]
        adjustments = [
            value[0]
            for value in db.execute(
                f"SELECT DISTINCT adjustment_status FROM observations WHERE normalized_symbol IN ({placeholders}) ORDER BY adjustment_status",  # noqa: S608
                aliases,
            )
        ]
        candidate_rows = db.execute(
            f"SELECT COUNT(*) FROM canonical_candidates WHERE normalized_symbol IN ({placeholders})",  # noqa: S608
            aliases,
        ).fetchone()[0]
        if not row[0]:
            status = "not_ready"
        elif row[3] or duplicate_count:
            status = "cleaning_required"
        elif conflicts or actions or symbol == "DSEX":
            status = "review_required"
        else:
            status = "ready_for_research_approval"
        dates = [
            value[0]
            for value in db.execute(
                f"SELECT DISTINCT trading_date FROM observations WHERE normalized_symbol IN ({placeholders}) AND trading_date IS NOT NULL ORDER BY trading_date",  # noqa: S608
                aliases,
            )
        ]
        calendar = calendar_analysis(dates)
        result.append(
            {
                "symbol": symbol,
                "coverage_start": row[1],
                "coverage_end": row[2],
                "observed_rows": row[0],
                "candidate_rows": candidate_rows,
                "missing_observed_market_dates": len(
                    calendar["missing_observed_market_dates"]
                ),
                "duplicates": duplicate_count,
                "invalid_rows": row[3] or 0,
                "cross_source_conflicts": conflicts,
                "corporate_action_candidates": actions,
                "adjusted_available": "adjusted" in adjustments,
                "unadjusted_available": "unadjusted" in adjustments,
                "unknown_adjustment_available": "unknown" in adjustments,
                "research_readiness_status": status,
                "automatically_approved": False,
            }
        )
    return result


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8"
    )


def _markdown(summary: dict[str, Any]) -> str:
    lines = [
        "# Canonical DSE Historical Research Candidate — Human Review Pack",
        "",
        "> **INACTIVE. HUMAN APPROVAL REQUIRED. QUALIFICATION: 0/60.**",
        "",
        "Raw files were hash-verified before and after processing and were not modified. Conflicting",
        "values were not averaged, deleted, or silently preferred.",
        "",
        "## Inventory",
        "",
        "| Dataset | Rows | Symbols | Duplicates | Invalid OHLC | Date coverage |",
        "|---|---:|---:|---:|---:|---|",
    ]
    for item in summary["dataset_inventory"]:
        lines.append(
            f"| {item['logical_name']} | {item['observed_row_count']} | {item['unique_symbols']} | "
            f"{item['duplicate_row_count']} | {item['invalid_ohlc_count']} | "
            f"{item['observed_start_date']} to {item['observed_end_date']} |"
        )
    lines.extend(
        [
            "",
            "## Stated-versus-observed mismatch",
            "",
            "The Mendeley page states 1,684,249 rows and more than 700 companies. The retained file",
            "is immutable and contains 1,523,921 parsed data rows and 529 distinct raw symbols. The",
            "difference is evidence of a publisher/file-description mismatch; neither claim is treated",
            "as authoritative without author confirmation and version-specific provenance.",
            "",
            "## Canonical candidate",
            "",
        ]
    )
    lines.extend(
        f"- {key}: {value}"
        for key, value in summary["canonical_candidate_counts"].items()
    )
    lines.extend(["", "## Proposed quality policy", ""])
    lines.extend(f"- {item}" for item in summary["proposed_canonical_quality_policy"])
    lines.extend(
        [
            "",
            "## Exact unresolved decisions",
            "",
        ]
    )
    lines.extend(f"- {item}" for item in summary["unresolved_decisions"])
    lines.extend(
        [
            "",
            "## Governance",
            "",
            "No normalized application data, source truth, rule claim, rule set, fee profile, risk",
            "limit, strategy, campaign, session, proposal, order, transaction, or fill was activated.",
            "The candidate is an inspectable review artifact only.",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build an inactive canonical DSE research candidate"
    )
    parser.add_argument("--tolerance", default="0.001")
    parser.add_argument("--operator", default="research-quality-builder")
    parser.add_argument(
        "--output-root", type=Path, default=ROOT / "reports" / "research_data_quality"
    )
    args = parser.parse_args()
    tolerance = Decimal(args.tolerance)
    if tolerance < 0 or tolerance > Decimal("0.10"):
        raise ValueError("Tolerance must be between 0 and 0.10")
    _assert_safety()
    raw_before = _raw_hashes()
    with SessionLocal() as app_db:
        if not verify_audit_chain(app_db):
            raise RuntimeError("Canonical audit chain is invalid")
        before = _operational_state(app_db)
        sources = _registered_sources(app_db)
    run_payload = {
        "sources": sorted({item.source_hash for item in sources}),
        "version": TRANSFORMATION_VERSION,
        "tolerance": str(tolerance),
    }
    run_id = canonical_hash(run_payload)[:16]
    output = args.output_root / f"canonical_candidate_{run_id}"
    output.mkdir(parents=True, exist_ok=True)
    candidate_db = output / "canonical_candidate.sqlite3"
    builder = CanonicalCandidateBuilder(candidate_db, output, tolerance=tolerance)
    for source in sources:
        _ingest(builder, source)
    builder.materialize_symbol_mappings()
    duplicate_counts = builder.analyze_duplicates()
    comparison_counts = builder.reconcile_sources()
    action_counts = builder.detect_corporate_actions()
    candidate_counts = builder.build_canonical_candidates()
    inventory = builder.inventory_report()
    quality = _source_quality(builder.db, inventory)
    calendars = _calendar_reports(builder.db)
    universe = _universe_readiness(builder.db)
    invalid = _invalid_summary(builder.db)
    mappings = mapping_rows(builder)

    _write_json(output / "dataset_inventory.json", inventory)
    write_csv(output / "dataset_inventory.csv", inventory, list(inventory[0]))
    _write_json(output / "invalid_row_classification.json", invalid)
    _write_json(output / "source_quality_scores.json", quality)
    write_csv(
        output / "source_quality_scores.csv",
        [
            {
                "logical_name": item["logical_name"],
                "dataset_id": item["dataset_id"],
                "score": item["score"],
                "components": json.dumps(item["components"]),
                "weights": json.dumps(item["weights"]),
                "rates": json.dumps(item["rates"]),
                "truth_established": item["truth_established"],
            }
            for item in quality
        ],
        [
            "logical_name",
            "dataset_id",
            "score",
            "components",
            "weights",
            "rates",
            "truth_established",
        ],
    )
    write_csv(
        output / "symbol_mapping_queue.csv",
        mappings,
        [
            "original_symbol",
            "normalized_symbol",
            "mapping_reason",
            "instrument_class",
            "confidence",
            "evidence_source",
            "approval_status",
            "effective_from",
            "effective_to",
        ],
    )
    _write_json(output / "calendar_observations.json", calendars)
    _write_json(output / "initial_universe_readiness.json", universe)
    write_csv(output / "initial_universe_readiness.csv", universe, list(universe[0]))
    duplicate_rows = _export_table(
        builder.db, "duplicate_groups", output / "duplicate_resolution.csv"
    )
    comparison_rows = _export_table(
        builder.db,
        "cross_source_comparisons",
        output / "cross_source_conflict_ledger.csv",
    )
    action_rows = _export_table(
        builder.db,
        "corporate_action_candidates",
        output / "corporate_action_candidates.csv",
    )
    invalid_rows = _export_table(
        builder.db,
        "observations",
        output / "invalid_rows.csv",
        where="invalid_categories != '[]'",
    )
    canonical_rows = int(
        builder.db.execute("SELECT COUNT(*) FROM canonical_candidates").fetchone()[0]
    )
    rejected_invalid = int(
        builder.db.execute(
            "SELECT COUNT(*) FROM observations WHERE accepted_for_candidate=0"
        ).fetchone()[0]
    )
    candidate_counts["materialized_candidate_rows"] = canonical_rows
    candidate_counts["rejected_invalid"] = rejected_invalid
    candidate_counts["corporate_action_pending"] = action_rows
    quality_policy = [
        "Preserve adjusted, unadjusted, and unknown-adjustment series as separate grains.",
        "Reject structural OHLCV/date/symbol invalidity; retain rejected rows and reasons in lineage.",
        "Collapse only exact same-source duplicates while retaining every contributing row ID.",
        "Exclude conflicting same-grain values; never average or silently prefer a source.",
        "Hold uncertain symbol mappings and all corporate-action candidates for human review.",
        "Treat third-party timestamps as unknown/provider-asserted, never exchange-verified.",
        "Keep every candidate inactive with pending_human_approval and qualification 0/60.",
    ]
    unresolved_decisions = [
        "Confirm the Mendeley stated row/symbol counts against the exact published version.",
        "Review every conflicting duplicate; only exact duplicates are collapse candidates.",
        "Establish adjusted/unadjusted semantics and corporate-action methodology per source.",
        "Approve or reject proposed symbol aliases, especially 00DSEX to DSEX.",
        "Obtain and verify an authoritative DSE trading calendar before calendar approval.",
        "Resolve cross-source OHLCV conflicts without averaging.",
        "Review suspected corporate actions against issuer/exchange announcements.",
        "Confirm licensing and automated-research rights for DSE Stocks and AmarStock.",
        "Approve the canonical quality policy before any research activation.",
    ]
    summary = {
        "run_id": run_id,
        "transformation_version": TRANSFORMATION_VERSION,
        "tolerance": str(tolerance),
        "dataset_inventory": inventory,
        "invalid_category_counts": {
            key: value["count"] for key, value in invalid.items()
        },
        "duplicate_resolution_counts": dict(duplicate_counts),
        "cross_source_comparison_counts": dict(comparison_counts),
        "source_quality_ranking": quality,
        "symbol_mapping_count": len(mappings),
        "calendar_observations": calendars,
        "corporate_action_candidate_counts": dict(action_counts),
        "initial_universe_readiness": universe,
        "canonical_candidate_counts": dict(candidate_counts),
        "proposed_canonical_quality_policy": quality_policy,
        "ledger_counts": {
            "invalid_rows": invalid_rows,
            "duplicate_groups": duplicate_rows,
            "cross_source_comparisons": comparison_rows,
            "corporate_action_candidates": action_rows,
        },
        "unresolved_decisions": unresolved_decisions,
        "activation_blocked": True,
        "qualification": "0/60",
        "raw_hashes_before": raw_before,
    }
    _write_json(output / "canonical_candidate_summary.json", summary)
    (output / "human_review_pack.md").write_text(_markdown(summary), encoding="utf-8")
    (output / "human_review_pack.html").write_text(
        review_html(summary), encoding="utf-8"
    )
    builder.close()
    raw_after = _raw_hashes()
    if raw_before != raw_after:
        raise RuntimeError("Raw source hash changed during candidate generation")
    with SessionLocal() as app_db:
        after_before_audit = _operational_state(app_db)
        delta = {key: after_before_audit[key] - before[key] for key in before}
        if any(delta.values()):
            raise RuntimeError(
                f"Operational state changed before audit recording: {delta}"
            )
        pack_files = sorted(
            path
            for path in output.iterdir()
            if path.name not in {"manifest.json", "audit_linkage.json"}
        )
        pack_hashes = {path.name: sha256_file(path) for path in pack_files}
        pack_hash = canonical_hash(pack_hashes)
        event = append_audit(
            app_db,
            actor=args.operator,
            event_type="research_dataset.canonical_candidate_generated",
            entity_type="canonical_research_candidate",
            entity_id=run_id,
            new_state={
                "pack_hash": pack_hash,
                "candidate_rows": canonical_rows,
                "active": False,
                "qualification": "0/60",
            },
        )
        app_db.commit()
        if not verify_audit_chain(app_db):
            raise RuntimeError("Audit chain failed after candidate evidence recording")
        after = _operational_state(app_db)
        final_delta = {key: after[key] - before[key] for key in before}
        if any(final_delta.values()):
            raise RuntimeError(f"Operational state changed: {final_delta}")
    audit_linkage = {
        "audit_event_id": event.id,
        "pack_hash": pack_hash,
        "active": False,
    }
    _write_json(output / "audit_linkage.json", audit_linkage)
    files = sorted(path for path in output.iterdir() if path.name != "manifest.json")
    manifest = {
        "run_id": run_id,
        "generated_at": datetime.now(UTC).isoformat(),
        "files": {
            path.name: {"sha256": sha256_file(path), "bytes": path.stat().st_size}
            for path in files
        },
        "raw_hashes_unchanged": raw_after,
        "operational_before": before,
        "operational_after": after,
        "operational_delta": final_delta,
        "audit_valid": True,
        "activation_blocked": True,
        "qualification": "0/60",
    }
    manifest["manifest_hash"] = canonical_hash(manifest)
    _write_json(output / "manifest.json", manifest)
    print(
        json.dumps(
            {
                "output": str(output),
                "manifest_hash": manifest["manifest_hash"],
                "candidate_rows": canonical_rows,
                "ledger_counts": summary["ledger_counts"],
                "operational_delta": final_delta,
                "audit_valid": True,
            },
            indent=2,
            default=str,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
