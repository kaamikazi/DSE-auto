from __future__ import annotations

import csv
import io
import json
import re
import zipfile
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, cast
from xml.etree import ElementTree

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    ApprovalPackRecord,
    AuthoritativeEvidence,
    EvidenceCollectionCase,
    EvidenceSourceProfile,
    ExtractedClaim,
    GovernanceItemApproval,
    Order,
    PaperSession,
    PortfolioStatementDraft,
    ResearchDataset,
    ReviewerInvitation,
    StrategyRegistration,
    Transaction,
    ValidationCampaign,
)
from app.services.audit import append_audit, verify_audit_chain
from app.services.authoritative_evidence import (
    canonical_hash,
    create_research_dataset,
    intake_evidence_file,
)
from app.services.portfolio_imports import FORBIDDEN_COLUMNS

CASE_STATES = {
    "planned",
    "awaiting_documents",
    "documents_received",
    "extraction_pending",
    "review_pending",
    "conflict_found",
    "ready_for_decision",
    "completed",
    "rejected",
    "archived",
}
CASE_TRANSITIONS = {
    "planned": {"awaiting_documents", "archived"},
    "awaiting_documents": {"documents_received", "rejected", "archived"},
    "documents_received": {"extraction_pending", "review_pending", "archived"},
    "extraction_pending": {"review_pending", "rejected", "archived"},
    "review_pending": {"conflict_found", "ready_for_decision", "rejected", "archived"},
    "conflict_found": {"review_pending", "ready_for_decision", "rejected", "archived"},
    "ready_for_decision": {"completed", "review_pending", "rejected", "archived"},
    "completed": {"archived"},
    "rejected": {"archived"},
    "archived": set(),
}
CLAIM_ACTIONS = {
    "accept": "accepted",
    "correct": "corrected",
    "reject": "rejected",
    "request_better_source": "better_source_requested",
    "mark_conflicting": "conflicting",
    "mark_obsolete": "obsolete",
}
SOURCE_CLASSES = {
    "official_exchange_publication": 10,
    "regulator_publication": 20,
    "official_broker_document": 30,
    "signed_account_statement": 40,
    "broker_customer_support_confirmation": 50,
    "listed_company_disclosure": 60,
    "licensed_data_vendor": 70,
    "audited_third_party_source": 80,
    "operator_attested_market_file": 90,
    "informal_webpage": 100,
    "social_media": 110,
    "unknown": 120,
}
DEFAULT_CASES = {
    "dse_market_rules": ["official DSE trading rule publication"],
    "trading_calendar_hours": ["official calendar", "official trading-hours notice"],
    "tick_sizes": ["official tick-size schedule"],
    "price_limits": ["official price-limit schedule"],
    "settlement": ["official settlement rules"],
    "suspensions": ["official suspension notices"],
    "corporate_actions": ["listed-company disclosures"],
    "broker_fee_schedule": ["official broker fee schedule"],
    "taxes_regulatory_deductions": ["regulator or broker deduction schedule"],
    "account_statements": ["signed or broker-issued account statement"],
    "transaction_history": ["broker transaction history"],
    "holdings": ["dated holdings statement"],
    "dividends_bonus_shares": ["statement or company disclosure"],
    "ohlcv_market_data": ["timestamped GP/ACI/BRACBANK market data"],
    "dsex_index_data": ["timestamped DSEX history"],
    "strategy_risk_review": ["strategy-specific independent risk review"],
}
WORKSPACE_ATTESTATION = (
    "I confirm these documents are described accurately, contain no credentials, and are "
    "submitted for review only; upload does not mean verification or approval."
)


def create_collection_case(
    db: Session,
    *,
    title: str,
    category: str,
    requested_documents: list[str],
    collector: str,
    reviewer: str | None = None,
    due_date: date | None = None,
    notes: str = "",
) -> EvidenceCollectionCase:
    case = EvidenceCollectionCase(
        title=title,
        evidence_category=category,
        requested_documents=requested_documents,
        missing_documents=list(requested_documents),
        responsible_collector=collector,
        reviewer=reviewer,
        due_date=due_date,
        notes=notes,
        state="planned",
    )
    db.add(case)
    db.flush()
    event = append_audit(
        db,
        actor=collector,
        event_type="evidence_case.created",
        entity_type="evidence_collection_case",
        entity_id=case.id,
        new_state={"category": category, "state": "planned"},
    )
    case.audit_event_ids = [event.id]
    db.commit()
    return case


def initialize_default_cases(
    db: Session, *, collector: str, reviewer: str | None
) -> list[EvidenceCollectionCase]:
    result: list[EvidenceCollectionCase] = []
    for category, documents in DEFAULT_CASES.items():
        existing = db.scalar(
            select(EvidenceCollectionCase).where(
                EvidenceCollectionCase.evidence_category == category,
                EvidenceCollectionCase.state != "archived",
            )
        )
        if existing:
            result.append(existing)
            continue
        result.append(
            create_collection_case(
                db,
                title=category.replace("_", " ").title(),
                category=category,
                requested_documents=documents,
                collector=collector,
                reviewer=reviewer,
            )
        )
    return result


def transition_case(
    db: Session,
    case: EvidenceCollectionCase,
    target_state: str,
    *,
    actor: str,
    notes: str = "",
) -> EvidenceCollectionCase:
    if target_state not in CASE_STATES:
        raise ValueError("Unknown evidence-case state")
    if target_state not in CASE_TRANSITIONS[case.state]:
        raise ValueError(f"Invalid evidence-case transition: {case.state} -> {target_state}")
    previous = case.state
    case.state = target_state
    case.updated_at = datetime.now(UTC)
    if notes:
        case.notes = f"{case.notes}\n{notes}".strip()
    event = append_audit(
        db,
        actor=actor,
        event_type="evidence_case.transitioned",
        entity_type="evidence_collection_case",
        entity_id=case.id,
        previous_state={"state": previous},
        new_state={"state": target_state, "notes": notes},
    )
    case.audit_event_ids = [*case.audit_event_ids, event.id]
    db.commit()
    return case


def create_source_profile(
    db: Session,
    *,
    name: str,
    source_class: str,
    authority_scope: list[str],
    account_applicability: list[str] | None = None,
    applicable_from: date | None = None,
    applicable_to: date | None = None,
    authenticity_review: str = "pending",
    confidence: str = "unknown",
) -> EvidenceSourceProfile:
    if source_class not in SOURCE_CLASSES:
        raise ValueError("Unknown evidence source class")
    existing = db.scalar(select(EvidenceSourceProfile).where(EvidenceSourceProfile.name == name))
    if existing:
        return existing
    profile = EvidenceSourceProfile(
        name=name,
        source_class=source_class,
        hierarchy_rank=SOURCE_CLASSES[source_class],
        authority_scope=authority_scope,
        applicable_from=applicable_from,
        applicable_to=applicable_to,
        account_applicability=account_applicability or [],
        authenticity_review=authenticity_review,
        confidence=confidence,
        conflicts=[],
        auto_verified=False,
    )
    db.add(profile)
    db.commit()
    return profile


def batch_intake(
    db: Session,
    *,
    case: EvidenceCollectionCase,
    files: list[dict[str, Any]],
    source_organization: str,
    source_class: str,
    source_description: str,
    operator_attestation: str,
    collected_by: str,
    raw_dir: Path,
    document_date: date | None = None,
    effective_date: date | None = None,
    account_or_broker_label: str | None = None,
) -> dict[str, Any]:
    if operator_attestation.strip() != WORKSPACE_ATTESTATION:
        raise ValueError(f"Operator attestation must match exactly: {WORKSPACE_ATTESTATION}")
    profile = create_source_profile(
        db,
        name=f"{source_organization}:{source_class}",
        source_class=source_class,
        authority_scope=[case.evidence_category],
        account_applicability=[account_or_broker_label] if account_or_broker_label else [],
    )
    accepted: list[AuthoritativeEvidence] = []
    errors: list[dict[str, str]] = []
    for upload in files:
        filename = str(upload["filename"])
        try:
            item = intake_evidence_file(
                db,
                category=case.evidence_category,
                title=str(upload.get("title") or filename),
                source_organization=source_organization,
                source_type=source_class,
                source_reference=str(upload.get("source_reference") or "operator_upload"),
                collected_by=collected_by,
                source_description=source_description,
                operator_attestation=operator_attestation,
                filename=filename,
                raw=cast(bytes, upload["raw"]),
                raw_dir=raw_dir,
                declared_type=cast(str | None, upload.get("declared_type")),
                document_date=document_date,
                effective_date=effective_date,
                affected_fields=[case.evidence_category],
                extraction={
                    "workspace_case_id": case.id,
                    "source_profile_id": profile.id,
                    "account_or_broker_label": account_or_broker_label,
                    "human_verified": False,
                },
            )
            accepted.append(item)
        except (KeyError, TypeError, ValueError) as exc:
            errors.append({"filename": filename, "error": str(exc)})
    if accepted:
        case.received_evidence_ids = [
            *case.received_evidence_ids,
            *[item.id for item in accepted],
        ]
        case.missing_documents = (
            [] if len(accepted) >= len(case.requested_documents) else case.missing_documents
        )
        if case.state == "planned":
            case.state = "awaiting_documents"
        if case.state == "awaiting_documents":
            case.state = "documents_received"
        case.updated_at = datetime.now(UTC)
        db.commit()
    return {
        "accepted": [
            {
                "id": item.id,
                "filename": item.original_filename,
                "sha256": item.file_hash,
                "media_type": item.media_type,
                "status": item.verification_status,
                "warning": "UPLOADED DOES NOT MEAN VERIFIED",
            }
            for item in accepted
        ],
        "errors": errors,
        "source_profile_id": profile.id,
        "case_state": case.state,
        "automatic_approval": False,
    }


def _column_index(reference: str) -> int:
    letters = "".join(char for char in reference if char.isalpha()).upper()
    result = 0
    for char in letters:
        result = result * 26 + ord(char) - 64
    return max(result - 1, 0)


def _xlsx_cells(raw: bytes) -> list[tuple[str, str, str]]:
    namespace = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    result: list[tuple[str, str, str]] = []
    with zipfile.ZipFile(io.BytesIO(raw)) as workbook:
        shared: list[str] = []
        if "xl/sharedStrings.xml" in workbook.namelist():
            root = ElementTree.fromstring(workbook.read("xl/sharedStrings.xml"))
            shared = ["".join(node.itertext()) for node in root.findall("x:si", namespace)]
        sheets = sorted(
            name
            for name in workbook.namelist()
            if name.startswith("xl/worksheets/sheet") and name.endswith(".xml")
        )
        for sheet in sheets:
            root = ElementTree.fromstring(workbook.read(sheet))
            for cell in root.findall(".//x:c", namespace):
                reference = cell.attrib.get("r", "?")
                value_node = cell.find("x:v", namespace)
                inline = cell.find("x:is", namespace)
                value = "" if value_node is None else value_node.text or ""
                if cell.attrib.get("t") == "s" and value.isdigit() and int(value) < len(shared):
                    value = shared[int(value)]
                elif inline is not None:
                    value = "".join(inline.itertext())
                if value:
                    result.append((Path(sheet).stem, reference, value))
    return result


def _tabular_rows(filename: str, raw: bytes) -> list[dict[str, str]]:
    suffix = Path(filename).suffix.lower()
    if suffix == ".csv":
        return [
            {str(key).strip(): str(value or "").strip() for key, value in row.items() if key}
            for row in csv.DictReader(io.StringIO(raw.decode("utf-8-sig")))
        ]
    if suffix != ".xlsx":
        raise ValueError("Tabular preview requires CSV or XLSX")
    cells = _xlsx_cells(raw)
    sheets: dict[str, dict[int, dict[int, str]]] = {}
    for sheet, reference, value in cells:
        row_number = int("".join(char for char in reference if char.isdigit()) or "0")
        sheets.setdefault(sheet, {}).setdefault(row_number, {})[_column_index(reference)] = value
    rows: list[dict[str, str]] = []
    for sheet, sheet_rows in sheets.items():
        if not sheet_rows:
            continue
        first = min(sheet_rows)
        headers = sheet_rows[first]
        for row_number in sorted(number for number in sheet_rows if number != first):
            rows.append(
                {
                    headers.get(column, f"column_{column + 1}"): value
                    for column, value in sheet_rows[row_number].items()
                }
                | {"_sheet": sheet, "_row": str(row_number)}
            )
    return rows


def _claim_type(field: str, value: str) -> str | None:
    normalized = re.sub(r"[^a-z0-9]+", "_", field.lower()).strip("_")
    aliases = {
        "trading_day": "weekly_trading_days",
        "trading_days": "weekly_trading_days",
        "market_time": "market_sessions",
        "market_hours": "market_sessions",
        "tick_size": "tick_sizes",
        "price_limit": "price_bands",
        "settlement_cycle": "settlement",
        "brokerage": "buy_brokerage",
        "buy_brokerage": "buy_brokerage",
        "sell_brokerage": "sell_brokerage",
        "minimum_charge": "minimum_brokerage",
        "tax": "tax",
        "symbol": "symbol",
        "quantity": "quantity",
        "average_cost": "average_acquisition_cost",
        "acquisition_price": "average_acquisition_cost",
        "transaction_date": "transaction_date",
        "dividend": "dividend",
        "bonus_share": "bonus_shares",
        "dsex": "dsex_value",
        "open": "open",
        "high": "high",
        "low": "low",
        "close": "close",
        "volume": "volume",
    }
    if normalized in aliases:
        return aliases[normalized]
    lower_value = value.lower()
    if "t+" in lower_value:
        return "settlement"
    if "%" in value and any(term in normalized for term in ("fee", "tax", "limit", "broker")):
        return normalized
    return None


def _text_candidates(text: str) -> list[tuple[str, str, str]]:
    candidates: list[tuple[str, str, str]] = []
    patterns = {
        "weekly_trading_days": re.compile(r"(?:trading days?|weekdays?)\s*[:=-]\s*([^\n]+)", re.I),
        "market_sessions": re.compile(
            r"(?:market|trading)\s*(?:hours?|time)\s*[:=-]\s*([^\n]+)", re.I
        ),
        "tick_sizes": re.compile(r"tick\s*size\s*[:=-]\s*([^\n]+)", re.I),
        "price_bands": re.compile(r"price\s*(?:limit|band)\s*[:=-]\s*([^\n]+)", re.I),
        "settlement": re.compile(r"settlement\s*[:=-]\s*([^\n]+)", re.I),
        "buy_brokerage": re.compile(r"buy\s*brokerage\s*[:=-]\s*([^\n]+)", re.I),
        "sell_brokerage": re.compile(r"sell\s*brokerage\s*[:=-]\s*([^\n]+)", re.I),
        "minimum_brokerage": re.compile(r"minimum\s*(?:brokerage|charge)\s*[:=-]\s*([^\n]+)", re.I),
    }
    for line_number, line in enumerate(text.splitlines(), 1):
        for claim_type, pattern in patterns.items():
            match = pattern.search(line)
            if match:
                candidates.append((claim_type, f"line {line_number}", match.group(1).strip()))
    return candidates


def deterministic_extract(
    db: Session,
    evidence: AuthoritativeEvidence,
    *,
    case: EvidenceCollectionCase | None = None,
    source_profile: EvidenceSourceProfile | None = None,
) -> list[ExtractedClaim]:
    if not evidence.raw_file_path:
        raise ValueError("Evidence has no retained source file")
    raw = Path(evidence.raw_file_path).read_bytes()
    suffix = Path(evidence.original_filename or "").suffix.lower()
    candidates: list[tuple[str, str, str]] = []
    method = "manual_transcription_required"
    if suffix in {".csv", ".xlsx"}:
        method = "deterministic_tabular"
        for row_number, row in enumerate(_tabular_rows(evidence.original_filename or "", raw), 2):
            location_prefix = (
                f"sheet {row.get('_sheet')} row {row.get('_row')}"
                if row.get("_sheet")
                else f"row {row_number}"
            )
            for field, value in row.items():
                if field.startswith("_") or not value:
                    continue
                claim_type = _claim_type(field, value)
                if claim_type:
                    candidates.append((claim_type, f"{location_prefix} column {field}", value))
    elif suffix in {".txt", ".md"}:
        method = "deterministic_text_patterns"
        candidates = _text_candidates(raw.decode("utf-8-sig"))
    elif suffix == ".pdf":
        method = "deterministic_pdf_literal_text"
        literals = re.findall(rb"\(([^()]*)\)\s*Tj", raw)
        text = "\n".join(item.decode("latin-1", errors="replace") for item in literals)
        candidates = _text_candidates(text)
    elif suffix in {".png", ".jpg", ".jpeg"}:
        method = "manual_transcription_required"
    claims: list[ExtractedClaim] = []
    for claim_type, location, original in candidates:
        claim = ExtractedClaim(
            evidence_id=evidence.id,
            case_id=case.id if case else None,
            source_profile_id=source_profile.id if source_profile else None,
            claim_type=claim_type,
            source_location=location,
            original_value=original,
            normalized_interpretation={"value": original.strip(), "unverified": True},
            confidence="medium" if method.startswith("deterministic") else "unknown",
            extraction_method=method,
            reviewer_status="pending",
            effective_date=evidence.effective_date,
        )
        db.add(claim)
        claims.append(claim)
    db.flush()
    evidence.extraction = {
        **evidence.extraction,
        "method": method,
        "claim_count": len(claims),
        "human_verified": False,
        "source_preserved": True,
    }
    if case:
        case.state = "review_pending" if claims else "extraction_pending"
        case.updated_at = datetime.now(UTC)
    event = append_audit(
        db,
        actor="system",
        event_type="evidence.extraction_completed",
        entity_type="authoritative_evidence",
        entity_id=evidence.id,
        new_state={"method": method, "claims": len(claims), "verified": False},
    )
    for claim in claims:
        claim.audit_event_ids = [event.id]
    db.commit()
    return claims


def add_manual_claim(
    db: Session,
    evidence: AuthoritativeEvidence,
    *,
    claim_type: str,
    source_location: str,
    original_value: str,
    normalized_interpretation: dict[str, Any],
    case_id: str | None = None,
) -> ExtractedClaim:
    claim = ExtractedClaim(
        evidence_id=evidence.id,
        case_id=case_id,
        claim_type=claim_type,
        source_location=source_location,
        original_value=original_value,
        normalized_interpretation={**normalized_interpretation, "unverified": True},
        confidence="unknown",
        extraction_method="manual_transcription",
        reviewer_status="pending",
        effective_date=evidence.effective_date,
    )
    db.add(claim)
    db.commit()
    return claim


def review_claim(
    db: Session,
    claim: ExtractedClaim,
    *,
    action: str,
    reviewer: str,
    notes: str,
    corrected_interpretation: dict[str, Any] | None = None,
    supporting_evidence_ids: list[str] | None = None,
) -> ExtractedClaim:
    if action not in CLAIM_ACTIONS:
        raise ValueError("Unsupported claim-review action")
    if action == "correct" and not corrected_interpretation:
        raise ValueError("Corrected interpretation is required")
    previous = claim.reviewer_status
    if corrected_interpretation is not None:
        claim.normalized_interpretation = {
            **corrected_interpretation,
            "corrected": True,
            "authoritative_verification": False,
        }
    claim.reviewer_status = CLAIM_ACTIONS[action]
    claim.reviewer = reviewer
    claim.reviewer_notes = notes
    claim.supporting_evidence_ids = supporting_evidence_ids or claim.supporting_evidence_ids
    claim.reviewed_at = datetime.now(UTC)
    event = append_audit(
        db,
        actor=reviewer,
        event_type="evidence_claim.reviewed",
        entity_type="extracted_claim",
        entity_id=claim.id,
        previous_state={"reviewer_status": previous},
        new_state={
            "reviewer_status": claim.reviewer_status,
            "extraction_accuracy_only": True,
            "configuration_approval": False,
        },
    )
    claim.audit_event_ids = [*claim.audit_event_ids, event.id]
    db.commit()
    return claim


def detect_claim_conflicts(db: Session, claim_type: str) -> list[ExtractedClaim]:
    claims = list(
        db.scalars(
            select(ExtractedClaim).where(
                ExtractedClaim.claim_type == claim_type,
                ExtractedClaim.reviewer_status.in_(["accepted", "corrected", "conflicting"]),
            )
        )
    )
    profiles = {
        profile.id: profile
        for profile in db.scalars(
            select(EvidenceSourceProfile).where(
                EvidenceSourceProfile.id.in_(
                    [claim.source_profile_id for claim in claims if claim.source_profile_id]
                )
            )
        )
    }
    conflicted: set[str] = set()
    for index, left in enumerate(claims):
        for right in claims[index + 1 :]:
            reasons: list[str] = []
            if left.normalized_interpretation.get("value") != right.normalized_interpretation.get(
                "value"
            ):
                reasons.append("different_values")
            if left.effective_date != right.effective_date:
                reasons.append("different_effective_dates")
            left_profile = profiles.get(left.source_profile_id or "")
            right_profile = profiles.get(right.source_profile_id or "")
            if left_profile and right_profile:
                if left_profile.hierarchy_rank != right_profile.hierarchy_rank:
                    reasons.append("source_hierarchy_difference")
                if set(left_profile.account_applicability) != set(
                    right_profile.account_applicability
                ):
                    reasons.append("account_specific_exception")
            if reasons:
                for item in (left, right):
                    item.reviewer_status = "conflicting"
                    item.conflict_reasons = sorted(set([*item.conflict_reasons, *reasons]))
                    conflicted.add(item.id)
    db.commit()
    return [claim for claim in claims if claim.id in conflicted]


def resolve_conflict(
    db: Session,
    claim: ExtractedClaim,
    *,
    reviewer: str,
    accepted: bool,
    notes: str,
    supporting_evidence_ids: list[str],
) -> ExtractedClaim:
    return review_claim(
        db,
        claim,
        action="accept" if accepted else "reject",
        reviewer=reviewer,
        notes=notes,
        supporting_evidence_ids=supporting_evidence_ids,
    )


def _claims_for_item(db: Session, item_key: str) -> list[ExtractedClaim]:
    return list(
        db.scalars(
            select(ExtractedClaim)
            .where(ExtractedClaim.claim_type == item_key)
            .order_by(ExtractedClaim.created_at)
        )
    )


def rule_decision_view(db: Session, row: GovernanceItemApproval) -> dict[str, Any]:
    if row.approval_type != "rule":
        raise ValueError("Rule decision view requires a rule item")
    claims = _claims_for_item(db, row.item_key)
    profiles = {profile.id: profile for profile in db.scalars(select(EvidenceSourceProfile))}
    return {
        "item": row.item_key,
        "current_draft_assumption": row.current_draft,
        "linked_evidence": sorted({claim.evidence_id for claim in claims}),
        "reviewed_claims": [
            claim_view(claim, profiles.get(claim.source_profile_id or ""))
            for claim in claims
            if claim.reviewer_status in {"accepted", "corrected"}
        ],
        "conflicting_claims": [
            claim_view(claim, profiles.get(claim.source_profile_id or ""))
            for claim in claims
            if claim.reviewer_status == "conflicting"
        ],
        "effective_dates": sorted(
            {claim.effective_date.isoformat() for claim in claims if claim.effective_date}
        ),
        "source_hierarchy": sorted(
            [
                {
                    "source": profile.name,
                    "class": profile.source_class,
                    "rank": profile.hierarchy_rank,
                    "auto_verified": False,
                }
                for profile in profiles.values()
                if any(claim.source_profile_id == profile.id for claim in claims)
            ],
            key=lambda item: int(item["rank"]),
        ),
        "recommended_conservative_interpretation": row.conservative_fallback,
        "unresolved_questions": row.missing_evidence
        or (
            ["resolve conflicting claims"]
            if any(claim.reviewer_status == "conflicting" for claim in claims)
            else []
        ),
        "approval_options": [
            "approve_this_item",
            "approve_conservative_fallback",
            "reject_this_item",
            "request_more_evidence",
        ],
        "system_may_approve": False,
    }


def _cost_example(value: Any, amount: Decimal, minimum: Any = None, flat: Any = None) -> str:
    try:
        percent = Decimal(str(value or "0")) / 100
        charge = amount * percent + Decimal(str(flat or "0"))
        if minimum is not None:
            charge = max(charge, Decimal(str(minimum)))
        return str(charge.quantize(Decimal("0.01")))
    except (InvalidOperation, ValueError):
        return "unavailable"


def fee_decision_view(db: Session, row: GovernanceItemApproval) -> dict[str, Any]:
    if row.approval_type != "fee":
        raise ValueError("Fee decision view requires a fee item")
    claims = _claims_for_item(db, row.item_key)
    source = cast(dict[str, Any], row.current_draft.get("source", {}))
    value = row.proposed_value.get("value")
    examples = {
        str(amount): _cost_example(
            value,
            Decimal(amount),
            source.get("minimum_fee"),
            source.get("flat_fee"),
        )
        for amount in ("5000", "10000", "50000", "100000", "500000")
    }
    return {
        "item": row.item_key,
        "current_placeholder": row.current_draft,
        "extracted_broker_values": [claim.normalized_interpretation for claim in claims],
        "account_applicability": source.get("broker_account_applicability", "unresolved"),
        "buy_sell_applicability": "buy"
        if "buy" in row.item_key
        else "sell"
        if "sell" in row.item_key
        else "both_or_unresolved",
        "minimum_or_flat_charges": {
            "minimum": source.get("minimum_fee"),
            "flat": source.get("flat_fee"),
        },
        "tax_treatment": source.get("tax_treatment", "unresolved"),
        "effective_dates": sorted(
            {claim.effective_date.isoformat() for claim in claims if claim.effective_date}
        ),
        "conflicting_evidence": [
            claim.id for claim in claims if claim.reviewer_status == "conflicting"
        ],
        "cost_examples_bdt": examples,
        "conservative_interpretation": row.conservative_fallback,
        "unresolved_questions": row.missing_evidence,
        "approval_status": row.approval_status,
        "automatic_approval": False,
    }


def claim_view(
    claim: ExtractedClaim, profile: EvidenceSourceProfile | None = None
) -> dict[str, Any]:
    return {
        "id": claim.id,
        "evidence_id": claim.evidence_id,
        "source_location": claim.source_location,
        "original_value": claim.original_value,
        "normalized_interpretation": claim.normalized_interpretation,
        "confidence": claim.confidence,
        "extraction_method": claim.extraction_method,
        "reviewer_status": claim.reviewer_status,
        "source_class": profile.source_class if profile else "unknown",
        "source_rank": profile.hierarchy_rank if profile else SOURCE_CLASSES["unknown"],
    }


def preview_portfolio_statement(
    db: Session,
    evidence: AuthoritativeEvidence,
    *,
    broker_label: str,
    account_label: str,
    statement_date: date,
) -> PortfolioStatementDraft:
    if not evidence.raw_file_path or not evidence.file_hash:
        raise ValueError("Portfolio statement requires retained evidence")
    raw = Path(evidence.raw_file_path).read_bytes()
    rows = _tabular_rows(evidence.original_filename or "", raw)
    if not rows:
        raise ValueError("Portfolio statement contains no rows")
    headers = {key.strip().lower() for row in rows for key in row if not key.startswith("_")}
    unsafe = sorted(headers & FORBIDDEN_COLUMNS)
    if unsafe:
        raise ValueError(f"Credential columns are forbidden: {', '.join(unsafe)}")
    duplicate = db.scalar(
        select(PortfolioStatementDraft).where(
            PortfolioStatementDraft.statement_hash == evidence.file_hash
        )
    )
    if duplicate:
        raise ValueError(f"Duplicate portfolio statement: {duplicate.id}")
    holdings: list[dict[str, Any]] = []
    transactions: list[dict[str, Any]] = []
    dividends: list[dict[str, Any]] = []
    bonus_shares: list[dict[str, Any]] = []
    cash_balance = Decimal("0")
    errors: list[str] = []
    for number, row in enumerate(rows, 2):
        kind = row.get("record_type", "holding").strip().lower()
        try:
            if kind == "cash":
                cash_balance += Decimal(row.get("cash_balance", row.get("amount", "0")))
            elif kind == "holding":
                holdings.append(
                    {
                        "symbol": row["symbol"].strip().upper(),
                        "quantity": str(Decimal(row["quantity"])),
                        "average_acquisition_cost": str(Decimal(row["average_acquisition_cost"])),
                        "market_value": row.get("market_value") or None,
                    }
                )
            elif kind == "transaction":
                transactions.append(dict(row))
            elif kind == "dividend":
                dividends.append(dict(row))
            elif kind == "bonus_share":
                bonus_shares.append(dict(row))
            else:
                errors.append(f"row {number}: unknown record_type {kind}")
        except (KeyError, InvalidOperation) as exc:
            errors.append(f"row {number}: {exc}")
    previous = db.scalar(
        select(PortfolioStatementDraft)
        .where(
            PortfolioStatementDraft.account_label == account_label,
            PortfolioStatementDraft.statement_date < statement_date,
            PortfolioStatementDraft.state != "reversed",
        )
        .order_by(PortfolioStatementDraft.statement_date.desc())
        .limit(1)
    )
    discrepancies: list[dict[str, Any]] = []
    if previous:
        old = {item["symbol"]: item for item in previous.parsed_data.get("holdings", [])}
        new = {item["symbol"]: item for item in holdings}
        for symbol in sorted(set(old) | set(new)):
            old_quantity = Decimal(str(old.get(symbol, {}).get("quantity", "0")))
            new_quantity = Decimal(str(new.get(symbol, {}).get("quantity", "0")))
            if old_quantity != new_quantity:
                discrepancies.append(
                    {
                        "symbol": symbol,
                        "previous_quantity": str(old_quantity),
                        "current_quantity": str(new_quantity),
                        "difference": str(new_quantity - old_quantity),
                    }
                )
    parsed = {
        "holdings": holdings,
        "cash_balance": str(cash_balance),
        "transactions": transactions,
        "dividends": dividends,
        "bonus_shares": bonus_shares,
        "fees_and_taxes_present": any("fees" in row or "taxes" in row for row in rows),
        "errors": errors,
    }
    reconciliation = {
        "holding_count": len(holdings),
        "transaction_count": len(transactions),
        "dividend_count": len(dividends),
        "bonus_share_count": len(bonus_shares),
        "cash_balance": str(cash_balance),
        "previous_statement_id": previous.id if previous else None,
        "discrepancy_count": len(discrepancies),
        "imported_to_portfolio": False,
    }
    draft = PortfolioStatementDraft(
        evidence_id=evidence.id,
        broker_label=broker_label,
        account_label=account_label,
        statement_date=statement_date,
        statement_hash=evidence.file_hash,
        parsed_data=parsed,
        reconciliation_summary=reconciliation,
        discrepancies=discrepancies,
        state="previewed" if not errors else "review_required",
    )
    db.add(draft)
    db.flush()
    event = append_audit(
        db,
        actor="operator",
        event_type="portfolio_statement.previewed",
        entity_type="portfolio_statement_draft",
        entity_id=draft.id,
        new_state={
            "statement_hash": evidence.file_hash,
            "account_label": account_label,
            "discrepancies": len(discrepancies),
            "transactions_created": 0,
        },
    )
    draft.audit_event_ids = [event.id]
    db.commit()
    return draft


def reverse_portfolio_statement_draft(
    db: Session, draft: PortfolioStatementDraft, *, actor: str
) -> PortfolioStatementDraft:
    if draft.state == "reversed":
        raise ValueError("Portfolio statement draft is already reversed")
    draft.state = "reversed"
    draft.reversed_at = datetime.now(UTC)
    event = append_audit(
        db,
        actor=actor,
        event_type="portfolio_statement.draft_reversed",
        entity_type="portfolio_statement_draft",
        entity_id=draft.id,
        new_state={"state": "reversed", "transactions_reversed": 0},
    )
    draft.audit_event_ids = [*draft.audit_event_ids, event.id]
    db.commit()
    return draft


def preview_market_dataset(
    db: Session,
    evidence: AuthoritativeEvidence,
    *,
    name: str,
    timestamp_trust: str,
    raw_dir: Path,
    normalized_dir: Path,
) -> ResearchDataset:
    if not evidence.raw_file_path:
        raise ValueError("Dataset evidence has no retained raw file")
    raw = Path(evidence.raw_file_path).read_bytes()
    filename = evidence.original_filename or "dataset.csv"
    if Path(filename).suffix.lower() == ".xlsx":
        rows = _tabular_rows(filename, raw)
        fields = sorted({key for row in rows for key in row if not key.startswith("_")})
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        writer.writerows(
            {key: value for key, value in row.items() if key in fields} for row in rows
        )
        raw = output.getvalue().encode()
        filename = f"{Path(filename).stem}.csv"
    dataset = create_research_dataset(
        db,
        name=name,
        raw=raw,
        filename=filename,
        source_evidence=evidence,
        timestamp_trust=timestamp_trust,
        raw_dir=raw_dir,
        normalized_dir=normalized_dir,
    )
    dataset.quality_report = {
        **dataset.quality_report,
        "schema_report": {
            "required_symbols": ["GP", "ACI", "BRACBANK", "DSEX"],
            "present_symbols": dataset.symbols,
        },
        "date_range_report": _dataset_date_range(dataset),
        "missing_session_report": dataset.quality_report.get("missing_days", []),
        "duplicate_report": dataset.quality_report.get("duplicates", []),
        "outlier_report": dataset.quality_report.get("outliers", []),
        "corporate_action_report": {
            "adjustments": dataset.quality_report.get("corporate_actions_applied", 0)
        },
        "timestamp_provenance_report": {
            "trust": timestamp_trust,
            "source_hash": dataset.source_hash,
        },
        "quality_score": _quality_score(dataset.quality_report),
        "research_activation_recommendation": "review_for_research_activation"
        if dataset.quality_report.get("passed")
        else "reject_or_correct",
        "automatically_activated": False,
        "campaign_qualification_days": 0,
    }
    db.commit()
    return dataset


def _dataset_date_range(dataset: ResearchDataset) -> dict[str, str | None]:
    rows = json.loads(Path(dataset.normalized_file_path).read_text(encoding="utf-8"))
    timestamps = sorted(str(row["timestamp"]) for row in rows)
    return {
        "start": timestamps[0] if timestamps else None,
        "end": timestamps[-1] if timestamps else None,
    }


def _quality_score(report: dict[str, Any]) -> int:
    penalty = (
        len(report.get("duplicates", [])) * 10
        + len(report.get("missing_days", [])) * 2
        + len(report.get("outliers", [])) * 5
        + len(report.get("errors", [])) * 10
    )
    return max(0, 100 - penalty)


def completeness_tracker(db: Session) -> dict[str, Any]:
    rows = list(db.scalars(select(GovernanceItemApproval)))
    claims = list(db.scalars(select(ExtractedClaim)))
    datasets = list(db.scalars(select(ResearchDataset)))
    invitations = list(db.scalars(select(ReviewerInvitation)))

    def status_for(row: GovernanceItemApproval) -> str:
        if row.approval_status in {"approved", "rejected"}:
            return row.approval_status
        item_claims = [claim for claim in claims if claim.claim_type == row.item_key]
        if any(claim.reviewer_status == "conflicting" for claim in item_claims):
            return "conflicting"
        if any(claim.reviewer_status in {"accepted", "corrected"} for claim in item_claims):
            return "reviewed"
        if item_claims:
            return "extracted"
        if row.evidence_ids:
            return "document_received"
        return "missing"

    matrix = {
        kind: [
            {
                "item": row.item_key,
                "status": status_for(row),
                "next_action": _next_action(status_for(row)),
            }
            for row in rows
            if row.approval_type == kind
        ]
        for kind in ("rule", "fee", "risk")
    }
    extras = [
        {
            "item": "strategy_specific_risk_review",
            "status": "reviewed"
            if any(
                claim.claim_type == "strategy_risk_review"
                and claim.reviewer_status in {"accepted", "corrected"}
                for claim in claims
            )
            else "missing",
        },
        {
            "item": "real_research_dataset",
            "status": "reviewed"
            if any(item.status == "approved_for_research" for item in datasets)
            else "document_received"
            if datasets
            else "missing",
        },
        {
            "item": "data_quality_review",
            "status": "reviewed"
            if any(
                item.quality_report.get("passed") and item.status == "approved_for_research"
                for item in datasets
            )
            else "missing",
        },
        {
            "item": "independent_reviewer",
            "status": "reviewed"
            if any(
                item.independence == "independent" and item.state == "accepted"
                for item in invitations
            )
            else "missing",
        },
        {"item": "promotion_decision", "status": "missing"},
        {"item": "campaign_decision", "status": "missing"},
    ]
    for item in extras:
        item["next_action"] = _next_action(str(item["status"]))
    all_items = [*matrix["rule"], *matrix["fee"], *matrix["risk"], *extras]
    return {
        "rules": matrix["rule"],
        "fees": matrix["fee"],
        "risk_limits": matrix["risk"],
        "additional_gates": extras,
        "counts": {
            status: sum(item["status"] == status for item in all_items)
            for status in (
                "missing",
                "document_received",
                "extracted",
                "reviewed",
                "conflicting",
                "decision_ready",
                "approved",
                "rejected",
                "expired",
            )
        },
        "exact_missing_items": [item["item"] for item in all_items if item["status"] == "missing"],
        "campaign_qualification": "0/60",
    }


def _next_action(status: str) -> str:
    return {
        "missing": "collect genuine document",
        "document_received": "extract claims",
        "extracted": "human review",
        "reviewed": "prepare item-specific decision",
        "conflicting": "resolve conflict with better source",
        "decision_ready": "obtain explicit item approval",
        "approved": "no action; monitor expiry",
        "rejected": "collect replacement evidence",
        "expired": "collect current evidence",
    }.get(status, "review required")


def generate_scoped_approval_pack(
    db: Session,
    *,
    scope: str,
    output_dir: Path,
    generated_by: str,
) -> ApprovalPackRecord:
    allowed = {
        "rules",
        "fees",
        "risk_limits",
        "real_dataset",
        "ma_crossover_promotion",
        "campaign_creation",
    }
    if scope not in allowed:
        raise ValueError("Unknown approval-pack scope")
    evidence = list(db.scalars(select(AuthoritativeEvidence)))
    profiles = list(db.scalars(select(EvidenceSourceProfile)))
    claims = list(db.scalars(select(ExtractedClaim)))
    approvals = list(db.scalars(select(GovernanceItemApproval)))
    tracker = completeness_tracker(db)
    kind = {"rules": "rule", "fees": "fee", "risk_limits": "risk"}.get(scope)
    scoped_rows = [row for row in approvals if kind is None or row.approval_type == kind]
    payload = {
        "scope": scope,
        "generated_by": generated_by,
        "decision_implied": False,
        "blanket_approval_allowed": False,
        "no_blanket_approval_warning": "Generating this pack grants no approval. Every item and later lifecycle decision requires explicit scope-specific authorization.",
        "evidence_hashes": [item.file_hash for item in evidence if item.file_hash],
        "source_hierarchy": [
            {
                "id": item.id,
                "name": item.name,
                "class": item.source_class,
                "rank": item.hierarchy_rank,
                "auto_verified": False,
            }
            for item in sorted(profiles, key=lambda value: value.hierarchy_rank)
        ],
        "reviewed_claims": [
            claim_view(item) for item in claims if item.reviewer_status in {"accepted", "corrected"}
        ],
        "conflicts": [claim_view(item) for item in claims if item.reviewer_status == "conflicting"],
        "missing_evidence": tracker["exact_missing_items"],
        "proposed_values": [
            {
                "item": row.item_key,
                "value": row.proposed_value,
                "approval_status": row.approval_status,
            }
            for row in scoped_rows
        ],
        "conservative_alternatives": [
            {"item": row.item_key, "fallback": row.conservative_fallback} for row in scoped_rows
        ],
        "reviewer_independence": "non_independent"
        if not any(
            item.independence == "independent" for item in db.scalars(select(ReviewerInvitation))
        )
        else "mixed",
        "exact_approval_scope": scope,
        "consequences": {
            "approval": "future explicit decision only",
            "rejection": "remain blocked",
            "no_decision": "remain blocked",
        },
        "campaign": {"created": False, "qualification": "0/60"},
        "automatic_activation": False,
    }
    digest = canonical_hash(payload)
    existing = db.scalar(select(ApprovalPackRecord).where(ApprovalPackRecord.pack_hash == digest))
    if existing:
        return existing
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{scope}_approval_pack_{digest[:12]}.json"
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")
    record = ApprovalPackRecord(
        scope=scope, payload=payload, pack_hash=digest, output_path=str(path), state="generated"
    )
    db.add(record)
    db.flush()
    event = append_audit(
        db,
        actor=generated_by,
        event_type="approval_pack.generated",
        entity_type="approval_pack_record",
        entity_id=record.id,
        new_state={"scope": scope, "hash": digest, "decision_implied": False},
    )
    record.audit_event_id = event.id
    db.commit()
    return record


def workspace_summary(db: Session) -> dict[str, Any]:
    cases = list(db.scalars(select(EvidenceCollectionCase)))
    claims = list(db.scalars(select(ExtractedClaim)))
    statements = list(db.scalars(select(PortfolioStatementDraft)))
    datasets = list(db.scalars(select(ResearchDataset)))
    packs = list(db.scalars(select(ApprovalPackRecord)))
    tracker = completeness_tracker(db)
    proof = {
        "campaigns": int(db.scalar(select(func.count()).select_from(ValidationCampaign)) or 0),
        "sessions": int(db.scalar(select(func.count()).select_from(PaperSession)) or 0),
        "orders": int(db.scalar(select(func.count()).select_from(Order)) or 0),
        "transactions_fills": int(db.scalar(select(func.count()).select_from(Transaction)) or 0),
        "promoted_strategies": int(
            db.scalar(
                select(func.count())
                .select_from(StrategyRegistration)
                .where(
                    StrategyRegistration.lifecycle_state.in_(["paper_candidate", "paper_active"])
                )
            )
            or 0
        ),
    }
    return {
        "paper_trading": True,
        "live_trading_enabled": False,
        "warning": "UPLOADED DOES NOT MEAN VERIFIED",
        "evidence_cases": {
            state: sum(case.state == state for case in cases) for state in CASE_STATES
        },
        "inbox": {
            "documents": int(
                db.scalar(select(func.count()).select_from(AuthoritativeEvidence)) or 0
            ),
            "submitted": int(
                db.scalar(
                    select(func.count())
                    .select_from(AuthoritativeEvidence)
                    .where(AuthoritativeEvidence.verification_status == "submitted")
                )
                or 0
            ),
        },
        "extraction_review": {
            status: sum(claim.reviewer_status == status for claim in claims)
            for status in {
                "pending",
                "accepted",
                "corrected",
                "rejected",
                "conflicting",
                "obsolete",
                "better_source_requested",
            }
        },
        "conflicts": sum(claim.reviewer_status == "conflicting" for claim in claims),
        "rule_decisions": tracker["rules"],
        "fee_decisions": tracker["fees"],
        "portfolio_statements": {
            "total": len(statements),
            "drafts": sum(item.state != "reversed" for item in statements),
        },
        "market_datasets": {"total": len(datasets), "automatically_activated": 0},
        "completeness": tracker["counts"],
        "approval_packs": {"total": len(packs), "decision_implied": False},
        "proof_no_activation": proof,
        "audit_valid": verify_audit_chain(db),
        "qualification": "0/60",
    }
