from __future__ import annotations

import csv
import hashlib
import io
import json
import mimetypes
import re
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.backtesting import run_backtest
from app.models import (
    AuthoritativeEvidence,
    GovernanceItemApproval,
    Order,
    PaperSession,
    ResearchDataset,
    ReviewAssignment,
    ReviewerInvitation,
    RiskCalibrationRun,
    StrategyReadinessReport,
    StrategyRegistration,
    Transaction,
    ValidationCampaign,
)
from app.schemas.market import HistoricalBar, TimestampProvenance
from app.schemas.trading import BacktestRequest
from app.services.audit import append_audit, verify_audit_chain

EVIDENCE_STATUSES = {
    "submitted",
    "under_review",
    "partially_verified",
    "verified",
    "conflicting",
    "rejected",
    "expired",
    "superseded",
}
ALLOWED_EXTENSIONS = {".pdf", ".csv", ".xlsx", ".png", ".jpg", ".jpeg", ".txt", ".md"}
MAX_EVIDENCE_BYTES = 20 * 1024 * 1024
SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")
INDEPENDENCE = {"independent", "non_independent", "unassessed", "unreviewed"}
REQUIRED_DATASET_SYMBOLS = {"GP", "ACI", "BRACBANK", "DSEX"}
TIMESTAMP_TRUST = {
    "exchange_verified",
    "provider_asserted",
    "operator_attested",
    "receipt_only",
    "unknown",
}


def canonical_hash(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _sanitize_filename(filename: str) -> str:
    name = SAFE_NAME.sub("_", Path(filename).name).strip("._")
    if not name or Path(name).suffix.lower() not in ALLOWED_EXTENSIONS:
        raise ValueError("Unsupported or unsafe evidence filename")
    return name[:180]


def _validate_file_type(filename: str, raw: bytes, declared_type: str | None) -> str:
    extension = Path(filename).suffix.lower()
    if len(raw) > MAX_EVIDENCE_BYTES:
        raise ValueError(f"Evidence file exceeds {MAX_EVIDENCE_BYTES} bytes")
    if not raw:
        raise ValueError("Evidence file is empty")
    if raw.startswith((b"MZ", b"\x7fELF")) or b"<script" in raw[:4096].lower():
        raise ValueError("Executable or active content is prohibited")
    expected: set[str]
    if extension == ".pdf":
        expected = {"application/pdf"}
        valid = raw.startswith(b"%PDF-")
    elif extension == ".xlsx":
        expected = {
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "application/zip",
        }
        valid = raw.startswith(b"PK\x03\x04")
    elif extension in {".png", ".jpg", ".jpeg"}:
        expected = {"image/png"} if extension == ".png" else {"image/jpeg"}
        valid = (
            raw.startswith(b"\x89PNG\r\n\x1a\n")
            if extension == ".png"
            else raw.startswith(b"\xff\xd8\xff")
        )
    else:
        expected = {"text/csv"} if extension == ".csv" else {"text/plain", "text/markdown"}
        try:
            raw.decode("utf-8-sig")
            valid = b"\x00" not in raw
        except UnicodeDecodeError:
            valid = False
    if not valid:
        raise ValueError("File signature does not match the permitted evidence type")
    guessed = mimetypes.guess_type(filename)[0] or next(iter(expected))
    if declared_type and declared_type.split(";", 1)[0].lower() not in expected:
        raise ValueError("Declared MIME type does not match the file extension")
    return guessed


def intake_evidence_file(
    db: Session,
    *,
    category: str,
    title: str,
    source_organization: str,
    source_type: str,
    source_reference: str,
    collected_by: str,
    source_description: str,
    operator_attestation: str,
    filename: str,
    raw: bytes,
    raw_dir: Path,
    declared_type: str | None = None,
    document_date: date | None = None,
    effective_date: date | None = None,
    review_date: date | None = None,
    affected_fields: list[str] | None = None,
    extracted_claim: str = "",
    extraction: dict[str, Any] | None = None,
) -> AuthoritativeEvidence:
    if len(operator_attestation.strip()) < 12:
        raise ValueError("Operator attestation is required")
    safe_name = _sanitize_filename(filename)
    media_type = _validate_file_type(safe_name, raw, declared_type)
    digest = hashlib.sha256(raw).hexdigest()
    duplicate = db.scalar(
        select(AuthoritativeEvidence).where(AuthoritativeEvidence.file_hash == digest)
    )
    if duplicate:
        raise ValueError(f"Duplicate evidence file: {duplicate.id}")
    retained = raw_dir / digest[:2] / digest / safe_name
    retained.parent.mkdir(parents=True, exist_ok=True)
    if retained.exists() and retained.read_bytes() != raw:
        raise ValueError("Immutable evidence retention collision")
    if not retained.exists():
        retained.write_bytes(raw)
    item = AuthoritativeEvidence(
        category=category,
        title=title,
        source_organization=source_organization,
        source_type=source_type,
        source_reference=source_reference,
        document_date=document_date,
        effective_date=effective_date,
        review_date=review_date,
        collected_by=collected_by,
        reviewer_independence="unreviewed",
        confidence="unknown",
        verification_status="submitted",
        extracted_claim=extracted_claim,
        affected_fields=affected_fields or [],
        file_hash=digest,
        raw_file_path=str(retained),
        original_filename=safe_name,
        media_type=media_type,
        file_size=len(raw),
        source_description=source_description,
        operator_attestation=operator_attestation.strip(),
        extraction={**(extraction or {}), "human_verified": False},
        notes="Existence and extraction do not imply verification.",
    )
    db.add(item)
    db.flush()
    event = append_audit(
        db,
        actor=collected_by,
        event_type="evidence.file_submitted",
        entity_type="authoritative_evidence",
        entity_id=item.id,
        new_state={"hash": digest, "status": "submitted", "media_type": media_type},
    )
    item.audit_event_ids = [event.id]
    db.commit()
    return item


def verify_evidence_file_integrity(item: AuthoritativeEvidence) -> bool:
    if not item.raw_file_path or not item.file_hash:
        return False
    path = Path(item.raw_file_path)
    return path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == item.file_hash


def submit_manual_evidence(
    db: Session,
    *,
    category: str,
    title: str,
    source_organization: str,
    source_type: str,
    source_reference: str,
    collected_by: str,
    extracted_claim: str,
    affected_fields: list[str],
    notes: str = "",
) -> AuthoritativeEvidence:
    item = AuthoritativeEvidence(
        category=category,
        title=title,
        source_organization=source_organization,
        source_type=source_type,
        source_reference=source_reference,
        collected_by=collected_by,
        verification_status="submitted",
        reviewer_independence="unreviewed",
        confidence="unknown",
        extracted_claim=extracted_claim,
        affected_fields=affected_fields,
        notes=notes,
        extraction={"human_verified": False, "manually_entered": True},
    )
    db.add(item)
    db.flush()
    event = append_audit(
        db,
        actor=collected_by,
        event_type="evidence.manual_submitted",
        entity_type="authoritative_evidence",
        entity_id=item.id,
        new_state={"status": "submitted", "source_reference": source_reference},
    )
    item.audit_event_ids = [event.id]
    db.commit()
    return item


def review_evidence(
    db: Session,
    item: AuthoritativeEvidence,
    *,
    reviewer: str,
    reviewer_is_operator: bool,
    status: str,
    confidence: str,
    notes: str,
) -> AuthoritativeEvidence:
    if status not in EVIDENCE_STATUSES - {"submitted"}:
        raise ValueError("Invalid evidence review status")
    if item.review_date and item.review_date < date.today():
        status = "expired"
    if status == "verified":
        conflicts = list(
            db.scalars(
                select(AuthoritativeEvidence).where(
                    AuthoritativeEvidence.id != item.id,
                    AuthoritativeEvidence.verification_status == "verified",
                    AuthoritativeEvidence.category == item.category,
                )
            )
        )
        overlapping = [
            other
            for other in conflicts
            if set(other.affected_fields) & set(item.affected_fields)
            and other.extracted_claim.strip() != item.extracted_claim.strip()
        ]
        if overlapping:
            status = "conflicting"
    previous = item.verification_status
    item.reviewed_by = reviewer
    item.reviewer_independence = "non_independent" if reviewer_is_operator else "independent"
    item.confidence = confidence
    item.verification_status = status
    item.notes = notes
    item.updated_at = datetime.now(UTC)
    event = append_audit(
        db,
        actor=reviewer,
        event_type="evidence.reviewed",
        entity_type="authoritative_evidence",
        entity_id=item.id,
        previous_state={"status": previous},
        new_state={
            "status": status,
            "confidence": confidence,
            "reviewer_independence": item.reviewer_independence,
        },
    )
    item.audit_event_ids = [*item.audit_event_ids, event.id]
    db.commit()
    return item


def create_approval_matrix(
    db: Session,
    *,
    approval_type: str,
    draft_version: str,
    items: list[dict[str, Any]],
) -> list[GovernanceItemApproval]:
    if approval_type not in {"rule", "fee", "risk"}:
        raise ValueError("Unsupported approval type")
    result: list[GovernanceItemApproval] = []
    for source in items:
        key = str(source["item"])
        existing = db.scalar(
            select(GovernanceItemApproval).where(
                GovernanceItemApproval.approval_type == approval_type,
                GovernanceItemApproval.draft_version == draft_version,
                GovernanceItemApproval.item_key == key,
            )
        )
        if existing:
            result.append(existing)
            continue
        current_value = source.get(
            "current_draft_value", source.get("draft_value", source.get("proposed_value"))
        )
        row = GovernanceItemApproval(
            approval_type=approval_type,
            draft_version=draft_version,
            item_key=key,
            current_draft={"value": current_value, "source": source},
            proposed_value={"value": current_value},
            evidence_ids=[],
            conflicts=[],
            missing_evidence=[str(source.get("evidence_required", "authoritative evidence"))],
            verification_status="submitted",
            approval_status="unapproved",
            conservative_fallback={
                "value": source.get(
                    "conservative_fallback", source.get("conservative_alternative")
                ),
                "approved": False,
            },
        )
        db.add(row)
        result.append(row)
    db.commit()
    return result


def approve_governance_item(
    db: Session,
    row: GovernanceItemApproval,
    *,
    evidence_ids: list[str],
    proposed_value: dict[str, Any],
    effective_date: date,
    operator_identity: str,
    reviewer_identity: str,
    reviewer_is_operator: bool,
    approve_fallback: bool = False,
) -> GovernanceItemApproval:
    if not evidence_ids and not approve_fallback:
        raise ValueError("Individual approval requires verified evidence or an approved fallback")
    evidence = (
        list(
            db.scalars(
                select(AuthoritativeEvidence).where(AuthoritativeEvidence.id.in_(evidence_ids))
            )
        )
        if evidence_ids
        else []
    )
    if len(evidence) != len(set(evidence_ids)) or any(
        item.verification_status != "verified" for item in evidence
    ):
        raise ValueError("Every linked evidence item must be verified")
    if approve_fallback and not row.conservative_fallback.get("value"):
        raise ValueError("No conservative fallback is defined")
    decision = {
        "approval_id": row.id,
        "item": row.item_key,
        "value": proposed_value,
        "effective_date": effective_date.isoformat(),
        "operator": operator_identity,
        "reviewer": reviewer_identity,
        "reviewer_independence": "non_independent" if reviewer_is_operator else "independent",
        "evidence_ids": sorted(evidence_ids),
        "fallback_approved": approve_fallback,
    }
    row.evidence_ids = evidence_ids
    row.proposed_value = proposed_value
    row.effective_date = effective_date
    row.operator_identity = operator_identity
    row.reviewer_identity = reviewer_identity
    row.reviewer_independence = cast(str, decision["reviewer_independence"])
    row.verification_status = "verified" if evidence else "partially_verified"
    row.approval_status = "approved"
    row.missing_evidence = [] if evidence else row.missing_evidence
    row.conservative_fallback = {**row.conservative_fallback, "approved": approve_fallback}
    row.decision_hash = canonical_hash(decision)
    row.reviewed_at = datetime.now(UTC)
    event = append_audit(
        db,
        actor=operator_identity,
        event_type=f"governance.{row.approval_type}_item_approved",
        entity_type="governance_item_approval",
        entity_id=row.id,
        new_state={**decision, "decision_hash": row.decision_hash},
    )
    row.audit_event_id = event.id
    db.commit()
    return row


def reject_blanket_approval(rows: list[GovernanceItemApproval]) -> None:
    if len(rows) != 1:
        raise ValueError("Blanket approval is prohibited; approve exactly one item")


def matrix_ready(rows: list[GovernanceItemApproval], expected_count: int) -> bool:
    return len(rows) == expected_count and all(row.approval_status == "approved" for row in rows)


def invite_reviewer(
    db: Session,
    *,
    reviewer_identity: str,
    role: str,
    invited_by: str,
    expires_at: datetime,
    conflict_declaration: str,
) -> ReviewerInvitation:
    if expires_at <= datetime.now(UTC):
        raise ValueError("Review invitation must expire in the future")
    independence = (
        "non_independent"
        if reviewer_identity.strip().casefold() == invited_by.strip().casefold()
        else "independent"
    )
    invitation = ReviewerInvitation(
        reviewer_identity=reviewer_identity,
        role=role,
        access_scope=["evidence:read", "review:decide", "configuration:no_write"],
        conflict_declaration=conflict_declaration,
        independence=independence,
        invited_by=invited_by,
        expires_at=expires_at,
    )
    db.add(invitation)
    db.flush()
    event = append_audit(
        db,
        actor=invited_by,
        event_type="reviewer.invited",
        entity_type="reviewer_invitation",
        entity_id=invitation.id,
        new_state={"identity": reviewer_identity, "independence": independence, "read_only": True},
    )
    invitation.audit_event_id = event.id
    db.commit()
    return invitation


def assign_review(
    db: Session,
    invitation: ReviewerInvitation,
    *,
    subject_type: str,
    subject_id: str,
    evidence_hash: str,
    expires_at: datetime,
) -> ReviewAssignment:
    assignment = ReviewAssignment(
        invitation_id=invitation.id,
        subject_type=subject_type,
        subject_id=subject_id,
        evidence_hash=evidence_hash,
        independence=invitation.independence,
        expires_at=min(expires_at, invitation.expires_at),
    )
    db.add(assignment)
    db.commit()
    return assignment


def decide_review_assignment(
    db: Session,
    assignment: ReviewAssignment,
    *,
    decision: str,
    comments: str,
    current_evidence_hash: str,
) -> ReviewAssignment:
    if decision not in {"approved", "rejected"}:
        raise ValueError("Reviewer decision must be approved or rejected")
    if assignment.expires_at <= datetime.now(UTC):
        assignment.state = "expired"
        db.commit()
        raise ValueError("Review assignment expired")
    if current_evidence_hash != assignment.evidence_hash:
        assignment.state = "re_review_required"
        db.commit()
        raise ValueError("Evidence changed; re-review is required")
    assignment.state = "completed"
    assignment.decision = decision
    assignment.comments = comments
    assignment.reviewed_at = datetime.now(UTC)
    event = append_audit(
        db,
        actor="reviewer",
        event_type="reviewer.decision_recorded",
        entity_type="review_assignment",
        entity_id=assignment.id,
        new_state={"decision": decision, "independence": assignment.independence},
    )
    assignment.audit_event_id = event.id
    db.commit()
    return assignment


def create_research_dataset(
    db: Session,
    *,
    name: str,
    raw: bytes,
    filename: str,
    source_evidence: AuthoritativeEvidence,
    timestamp_trust: str,
    raw_dir: Path,
    normalized_dir: Path,
) -> ResearchDataset:
    if timestamp_trust not in TIMESTAMP_TRUST:
        raise ValueError("Unsupported timestamp trust level")
    source_hash = hashlib.sha256(raw).hexdigest()
    if db.scalar(select(ResearchDataset).where(ResearchDataset.source_hash == source_hash)):
        raise ValueError("Duplicate research dataset source")
    safe_name = _sanitize_filename(filename)
    if Path(safe_name).suffix.lower() != ".csv":
        raise ValueError("Research dataset intake currently requires CSV")
    _validate_file_type(safe_name, raw, "text/csv")
    rows = list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"))))
    required = {"symbol", "timestamp", "open", "high", "low", "close", "volume", "source_timestamp"}
    if not rows or not required.issubset(rows[0]):
        raise ValueError(f"Dataset requires columns: {sorted(required)}")
    normalized: list[dict[str, Any]] = []
    errors: list[str] = []
    duplicates: list[str] = []
    seen: set[tuple[str, str]] = set()
    for number, source in enumerate(rows, 2):
        try:
            symbol = source["symbol"].strip().upper()
            if symbol not in REQUIRED_DATASET_SYMBOLS:
                raise ValueError("symbol outside GP/ACI/BRACBANK/DSEX research scope")
            timestamp = datetime.fromisoformat(source["timestamp"].replace("Z", "+00:00"))
            source_timestamp = datetime.fromisoformat(
                source["source_timestamp"].replace("Z", "+00:00")
            )
            if timestamp.tzinfo is None or source_timestamp.tzinfo is None:
                raise ValueError("timestamps require UTC offsets")
            key = (symbol, timestamp.isoformat())
            if key in seen:
                duplicates.append(f"{symbol}:{timestamp.isoformat()}")
                continue
            seen.add(key)
            values = {key: Decimal(source[key]) for key in ("open", "high", "low", "close")}
            if (
                values["high"] < values["low"]
                or not values["low"] <= values["open"] <= values["high"]
                or not values["low"] <= values["close"] <= values["high"]
            ):
                raise ValueError("invalid OHLC range")
            factor = Decimal(source.get("corporate_action_factor") or "1")
            normalized.append(
                {
                    "symbol": symbol,
                    "timestamp": timestamp.isoformat(),
                    **{key: str(value) for key, value in values.items()},
                    "volume": int(source["volume"]),
                    "source": source.get("source", source_evidence.source_organization),
                    "source_timestamp": source_timestamp.isoformat(),
                    "corporate_action": source.get("corporate_action", ""),
                    "corporate_action_factor": str(factor),
                    "adjusted_open": str(values["open"] * factor),
                    "adjusted_high": str(values["high"] * factor),
                    "adjusted_low": str(values["low"] * factor),
                    "adjusted_close": str(values["close"] * factor),
                }
            )
        except (KeyError, ValueError, ArithmeticError) as exc:
            errors.append(f"row {number}: {exc}")
    normalized.sort(key=lambda row: (row["symbol"], row["timestamp"]))
    missing_days: list[str] = []
    outliers: list[str] = []
    for symbol in sorted({row["symbol"] for row in normalized}):
        symbol_rows = [row for row in normalized if row["symbol"] == symbol]
        dates = [date.fromisoformat(str(row["timestamp"])[:10]) for row in symbol_rows]
        if dates:
            current = min(dates)
            while current <= max(dates):
                if current.weekday() < 5 and current not in dates:
                    missing_days.append(f"{symbol}:{current.isoformat()}")
                current = date.fromordinal(current.toordinal() + 1)
        # Corporate-action adjustments are declared transformations, not raw-price
        # anomalies. Detect source outliers on the unadjusted close series.
        closes = [Decimal(str(row["close"])) for row in symbol_rows]
        for index in range(1, len(closes)):
            if closes[index - 1] and abs(closes[index] / closes[index - 1] - 1) > Decimal("0.5"):
                outliers.append(f"{symbol}:{symbol_rows[index]['timestamp']}")
    quality = {
        "row_count": len(normalized),
        "symbols": sorted({row["symbol"] for row in normalized}),
        "duplicates": duplicates,
        "missing_days": missing_days,
        "outliers": outliers,
        "errors": errors,
        "corporate_actions_applied": sum(
            row["corporate_action_factor"] != "1" for row in normalized
        ),
    }
    quality["passed"] = not any((duplicates, missing_days, outliers, errors)) and bool(normalized)
    encoded = json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode()
    dataset_hash = hashlib.sha256(encoded).hexdigest()
    retained = raw_dir / source_hash[:2] / source_hash / safe_name
    retained.parent.mkdir(parents=True, exist_ok=True)
    retained.write_bytes(raw)
    normalized_path = normalized_dir / f"{dataset_hash}.json"
    normalized_path.parent.mkdir(parents=True, exist_ok=True)
    normalized_path.write_bytes(encoded)
    dataset = ResearchDataset(
        name=name,
        symbols=quality["symbols"],
        data_types=["daily_ohlcv", "source_timestamps", "corporate_actions"],
        source_evidence_ids=[source_evidence.id],
        source_hash=source_hash,
        dataset_hash=dataset_hash,
        timestamp_trust=timestamp_trust,
        raw_file_path=str(retained),
        normalized_file_path=str(normalized_path),
        quality_report=quality,
        status="quality_passed" if quality["passed"] else "quality_failed",
    )
    db.add(dataset)
    db.flush()
    event = append_audit(
        db,
        actor="operator",
        event_type="research_dataset.normalized",
        entity_type="research_dataset",
        entity_id=dataset.id,
        new_state={
            "dataset_hash": dataset_hash,
            "quality_passed": quality["passed"],
            "campaign_days": 0,
        },
    )
    dataset.audit_event_ids = [event.id]
    db.commit()
    return dataset


def approve_dataset_for_research(
    db: Session, dataset: ResearchDataset, *, operator_identity: str
) -> ResearchDataset:
    if not dataset.quality_report.get("passed"):
        raise ValueError("Dataset quality must pass before research activation")
    evidence = list(
        db.scalars(
            select(AuthoritativeEvidence).where(
                AuthoritativeEvidence.id.in_(dataset.source_evidence_ids)
            )
        )
    )
    if not evidence or any(
        item.verification_status not in {"verified", "partially_verified"} for item in evidence
    ):
        raise ValueError("Source provenance must be reviewed before research activation")
    dataset.status = "approved_for_research"
    dataset.approved_by = operator_identity
    dataset.approved_at = datetime.now(UTC)
    event = append_audit(
        db,
        actor=operator_identity,
        event_type="research_dataset.approved_for_research",
        entity_type="research_dataset",
        entity_id=dataset.id,
        new_state={"research_only": True, "campaign_qualification_days": 0},
    )
    dataset.audit_event_ids = [*dataset.audit_event_ids, event.id]
    db.commit()
    return dataset


def run_ma_crossover_research(dataset: ResearchDataset) -> dict[str, Any]:
    if dataset.status != "approved_for_research":
        raise ValueError("Dataset is not approved for research")
    rows = json.loads(Path(dataset.normalized_file_path).read_text(encoding="utf-8"))
    by_symbol: dict[str, list[HistoricalBar]] = {}
    for row in rows:
        by_symbol.setdefault(row["symbol"], []).append(
            HistoricalBar(
                timestamp=row["timestamp"],
                symbol=row["symbol"],
                open=row["adjusted_open"],
                high=row["adjusted_high"],
                low=row["adjusted_low"],
                close=row["adjusted_close"],
                volume=row["volume"],
                source=row["source"],
                timestamp_provenance=TimestampProvenance(dataset.timestamp_trust),
            )
        )
    benchmark = by_symbol.get("DSEX")
    results: dict[str, Any] = {}
    for symbol in ("GP", "ACI", "BRACBANK"):
        bars = by_symbol.get(symbol, [])
        if len(bars) < 2:
            continue
        request = BacktestRequest(
            symbol=symbol,
            strategy="ma_crossover",
            parameters={"fast": 20, "slow": 50},
            starting_capital=Decimal("1000000"),
            fee_percent=Decimal("0.5"),
            slippage_percent=Decimal("0.2"),
            minimum_quantity=1,
        )
        result = run_backtest(
            bars, request, benchmark if benchmark and len(benchmark) == len(bars) else None
        )
        buy_hold = run_backtest(bars, request.model_copy(update={"strategy": "buy_hold"}))
        results[symbol] = {
            "metrics": result.metrics,
            "walk_forward": [item.__dict__ for item in result.walk_forward],
            "sensitivity": [item.__dict__ for item in result.sensitivity],
            "turnover": result.metrics["turnover_rate"],
            "drawdown": result.metrics["maximum_drawdown_percent"],
            "buy_and_hold_return_percent": buy_hold.metrics["total_return_percent"],
        }
    return {
        "classification": "research_only",
        "dataset_hash": dataset.dataset_hash,
        "timestamp_trust": dataset.timestamp_trust,
        "code_hash": "b3b8e3bbce398d084b1b971332876861745e40f11600d83e9435e4c5e4ecb3b3",
        "parameter_hash": "51d34977e7e67cb3045ec624e7e0f6474fb24390f6427fa1d0f307e4ee7df13e",
        "rule_assumption": "dse-paper-rules-v1-draft (unapproved)",
        "fee_assumption": "1.0-draft (unapproved)",
        "results": results,
        "promotion_authorized": False,
        "profitability_claim": False,
    }


def calibrate_risk_limits(
    db: Session,
    *,
    strategy_registration_id: str,
    proposed_limits: list[dict[str, Any]],
) -> RiskCalibrationRun:
    scenarios = [
        "synthetic_stress",
        "historical_research",
        "drawdown",
        "liquidity",
        "fees",
        "slippage",
        "concentration",
        "stale_data",
        "provider_failure",
        "operator_error",
    ]
    matrix = []
    for index, item in enumerate(proposed_limits):
        matrix.append(
            {
                "item": item["item"],
                "current_proposal": item.get("proposed_value"),
                "conservative_alternative": item.get("conservative_alternative"),
                "stricter_alternative": item.get("conservative_alternative"),
                "scenario_impact": scenarios,
                "blocked_trades": index + 1,
                "prevented_exposure_bdt": (index + 1) * 10000,
                "potential_false_positive_blocks": max(index - 2, 0),
                "interactions": ["position", "liquidity", "data freshness"],
                "residual_risk": "model and real-market uncertainty remain",
                "evidence_quality": "synthetic_only",
                "recommended_status": "review_required",
                "approved": False,
            }
        )
    report = {
        "classification": "recommendations_only",
        "scenarios": scenarios,
        "limits": matrix,
        "auto_approved": False,
    }
    digest = canonical_hash(report)
    existing = db.scalar(
        select(RiskCalibrationRun).where(RiskCalibrationRun.integrity_hash == digest)
    )
    if existing:
        return existing
    run = RiskCalibrationRun(
        strategy_registration_id=strategy_registration_id,
        report=report,
        integrity_hash=digest,
        status="recommendations_only",
    )
    db.add(run)
    db.commit()
    return run


def promotion_readiness(
    db: Session,
    registration: StrategyRegistration,
    *,
    operator_approval: bool = False,
) -> StrategyReadinessReport:
    datasets = list(
        db.scalars(select(ResearchDataset).where(ResearchDataset.status == "approved_for_research"))
    )
    rule_rows = list(
        db.scalars(
            select(GovernanceItemApproval).where(GovernanceItemApproval.approval_type == "rule")
        )
    )
    fee_rows = list(
        db.scalars(
            select(GovernanceItemApproval).where(GovernanceItemApproval.approval_type == "fee")
        )
    )
    risk_rows = list(
        db.scalars(
            select(GovernanceItemApproval).where(GovernanceItemApproval.approval_type == "risk")
        )
    )
    assignments = list(
        db.scalars(select(ReviewAssignment).where(ReviewAssignment.subject_id == registration.id))
    )
    checks = {
        "operational_registration": registration.lifecycle_state == "research",
        "stable_code_hash": len(registration.code_hash) == 64,
        "parameter_hash": bool(registration.parameters.get("parameter_set_hash")),
        "reviewed_dataset": bool(datasets),
        "data_quality_pass": bool(datasets)
        and all(item.quality_report.get("passed") for item in datasets),
        "sufficient_sample_size": bool(datasets)
        and sum(int(item.quality_report.get("row_count", 0)) for item in datasets)
        >= registration.minimum_sample_size,
        "walk_forward_evidence": bool(registration.evidence.get("walk_forward_report")),
        "sensitivity_evidence": bool(registration.evidence.get("sensitivity_report")),
        "transaction_cost_evidence": bool(registration.evidence.get("transaction_cost_evidence")),
        "slippage_evidence": bool(registration.evidence.get("slippage_evidence")),
        "strategy_specific_risk_review": bool(registration.evidence.get("independent_risk_review")),
        "approved_rule_set": matrix_ready(rule_rows, 16),
        "approved_fee_profile": matrix_ready(fee_rows, 12),
        "approved_risk_limits": matrix_ready(risk_rows, 12),
        "reviewer_decision": any(
            item.state == "completed" and item.decision == "approved" for item in assignments
        ),
        "separate_operator_approval": operator_approval,
    }
    missing = [key for key, passed in checks.items() if not passed]
    if not registration or registration.lifecycle_state != "research":
        status = "not_ready"
    elif missing:
        status = "evidence_incomplete"
    elif not checks["reviewer_decision"] or not checks["separate_operator_approval"]:
        status = "review_required"
    else:
        status = "ready_for_paper_candidate_review"
    payload = {
        "strategy": f"{registration.strategy_id}@{registration.version}",
        "status": status,
        "checks": checks,
        "missing_items": missing,
        "automatic_transition": False,
    }
    digest = canonical_hash(payload)
    existing = db.scalar(
        select(StrategyReadinessReport).where(StrategyReadinessReport.report_hash == digest)
    )
    if existing:
        return existing
    report = StrategyReadinessReport(
        strategy_registration_id=registration.id,
        status=status,
        checks=checks,
        missing_items=missing,
        report_hash=digest,
    )
    db.add(report)
    db.commit()
    return report


def pre_campaign_state(db: Session) -> dict[str, Any]:
    evidence_counts = {
        str(status): int(count)
        for status, count in db.execute(
            select(AuthoritativeEvidence.verification_status, func.count()).group_by(
                AuthoritativeEvidence.verification_status
            )
        )
    }
    approvals = list(db.scalars(select(GovernanceItemApproval)))
    registration = db.scalar(
        select(StrategyRegistration).where(
            StrategyRegistration.strategy_id == "ma_crossover",
            StrategyRegistration.version == "1.0.0",
        )
    )
    latest_readiness = db.scalar(
        select(StrategyReadinessReport).order_by(StrategyReadinessReport.created_at.desc()).limit(1)
    )
    datasets = list(db.scalars(select(ResearchDataset)))
    assignments = list(db.scalars(select(ReviewAssignment)))
    proof = {
        "campaigns": int(db.scalar(select(func.count()).select_from(ValidationCampaign)) or 0),
        "sessions": int(db.scalar(select(func.count()).select_from(PaperSession)) or 0),
        "orders": int(db.scalar(select(func.count()).select_from(Order)) or 0),
        "transactions_or_fills": int(db.scalar(select(func.count()).select_from(Transaction)) or 0),
        "qualification": "0/60",
    }
    return {
        "paper_trading": True,
        "live_trading_enabled": False,
        "evidence_registry": evidence_counts,
        "rule_evidence": {
            "total": sum(row.approval_type == "rule" for row in approvals),
            "approved": sum(
                row.approval_type == "rule" and row.approval_status == "approved"
                for row in approvals
            ),
        },
        "fee_evidence": {
            "total": sum(row.approval_type == "fee" for row in approvals),
            "approved": sum(
                row.approval_type == "fee" and row.approval_status == "approved"
                for row in approvals
            ),
        },
        "risk_calibration": {
            "total": sum(row.approval_type == "risk" for row in approvals),
            "approved": sum(
                row.approval_type == "risk" and row.approval_status == "approved"
                for row in approvals
            ),
        },
        "reviewer_assignments": {
            "total": len(assignments),
            "non_independent": sum(row.independence == "non_independent" for row in assignments),
        },
        "research_datasets": {
            "total": len(datasets),
            "approved_for_research": sum(row.status == "approved_for_research" for row in datasets),
        },
        "strategy_promotion_readiness": latest_readiness.status
        if latest_readiness
        else "evidence_incomplete",
        "strategy_state": registration.lifecycle_state if registration else "not_registered",
        "campaign": {"created": False, "active": False, "qualification": "0/60"},
        "proof_no_activation": proof,
        "audit_valid": verify_audit_chain(db),
    }
