from __future__ import annotations

import csv
import hashlib
import io
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ImportBatch, Transaction
from app.schemas.trading import TransactionCreate
from app.services.audit import append_audit
from app.services.portfolio import add_transaction

PORTFOLIO_ATTESTATION = (
    "I confirm this portfolio file represents the stated broker/account and statement date, "
    "contains no credentials, and is for read-only comparison and reconciliation."
)
FORBIDDEN_COLUMNS = {
    "password",
    "broker_password",
    "trading_password",
    "pin",
    "otp",
    "api_key",
    "api_secret",
    "token",
    "credential",
}


def preview_import(db: Session, filename: str, raw: bytes) -> dict[str, Any]:
    digest = hashlib.sha256(raw).hexdigest()
    duplicate = db.scalar(select(ImportBatch).where(ImportBatch.source_hash == digest))
    rows = list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"))))
    valid: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for number, row in enumerate(rows, 2):
        try:
            valid.append(TransactionCreate.model_validate(row).model_dump(mode="json"))
        except Exception as exc:
            errors.append({"row": number, "error": str(exc)})
    return {
        "filename": filename,
        "source_hash": digest,
        "duplicate_batch_id": duplicate.id if duplicate else None,
        "rows": len(rows),
        "valid_rows": valid,
        "errors": errors,
        "source_untouched": True,
    }


def commit_import(db: Session, filename: str, raw: bytes) -> ImportBatch:
    preview = preview_import(db, filename, raw)
    if preview["duplicate_batch_id"] or preview["errors"]:
        raise ValueError("Import has duplicates or validation errors; preview required")
    batch = ImportBatch(
        source_name=filename,
        source_hash=preview["source_hash"],
        status="committed",
        row_count=preview["rows"],
    )
    db.add(batch)
    ids: list[str] = []
    for row in preview["valid_rows"]:
        payload = TransactionCreate.model_validate(row)
        ids.append(
            add_transaction(
                db, payload, source_record={"import_batch_id": batch.id, "original": row}
            ).id
        )
    batch.transaction_ids = ids
    append_audit(
        db,
        actor="operator",
        event_type="portfolio_import.committed",
        entity_type="import_batch",
        entity_id=batch.id,
        new_state={"rows": len(ids), "source_hash": batch.source_hash},
    )
    db.commit()
    return batch


def reverse_import(db: Session, batch: ImportBatch) -> None:
    if batch.status != "committed":
        raise ValueError("Only committed imports may be reversed")
    for tx_id in batch.transaction_ids:
        original = db.get(Transaction, tx_id)
        if original:
            if original.transaction_type == "cash_balance":
                db.add(
                    Transaction(
                        occurred_at=max(
                            datetime.now(UTC),
                            (
                                original.occurred_at.replace(tzinfo=UTC)
                                if original.occurred_at.tzinfo is None
                                else original.occurred_at
                            )
                            + timedelta(microseconds=1),
                        ),
                        transaction_type="cash_balance",
                        symbol="BDT",
                        quantity=0,
                        price=0,
                        fees=0,
                        taxes=0,
                        broker="reversal",
                        account_label=original.account_label,
                        notes=f"Reversal of {original.id}",
                        source_record={"import_batch_id": batch.id, "reverses": original.id},
                    )
                )
                continue
            db.add(
                Transaction(
                    occurred_at=datetime.now(UTC),
                    transaction_type="adjustment",
                    symbol=original.symbol,
                    quantity=-original.quantity,
                    price=-original.price,
                    fees=-original.fees,
                    taxes=-original.taxes,
                    broker="reversal",
                    account_label=original.account_label,
                    notes=f"Reversal of {original.id}",
                    source_record={"import_batch_id": batch.id, "reverses": original.id},
                )
            )
    batch.status = "reversed"
    batch.reversed_at = datetime.now(UTC)
    append_audit(
        db,
        actor="operator",
        event_type="portfolio_import.reversed",
        entity_type="import_batch",
        entity_id=batch.id,
    )
    db.commit()


def preview_real_portfolio(
    db: Session,
    filename: str,
    raw: bytes,
    *,
    statement_date: str,
    source_description: str,
    attestation: str,
) -> dict[str, Any]:
    if attestation.strip() != PORTFOLIO_ATTESTATION:
        raise ValueError(f"Portfolio attestation must match exactly: {PORTFOLIO_ATTESTATION}")
    if not statement_date or len(source_description.strip()) < 8:
        raise ValueError("Statement date and source document description are required")
    header = next(csv.reader(io.StringIO(raw.decode("utf-8-sig"))), [])
    unsafe = sorted({item.strip().lower() for item in header} & FORBIDDEN_COLUMNS)
    if unsafe:
        raise ValueError(f"Credential columns are forbidden: {', '.join(unsafe)}")
    preview = preview_import(db, filename, raw)
    for row in preview["valid_rows"]:
        if row.get("account_label") in {None, "", "paper", "simulation"}:
            preview["errors"].append(
                {"row": 0, "error": "Real portfolio rows require a non-paper account label"}
            )
    return {
        **preview,
        "statement_date": statement_date,
        "source_description": source_description.strip(),
        "attestation": attestation.strip(),
        "activation_allowed": not preview["errors"] and preview["duplicate_batch_id"] is None,
        "paper_holdings_affected": False,
        "real_order_submission": False,
    }


def activate_real_portfolio(
    db: Session,
    filename: str,
    raw: bytes,
    *,
    statement_date: str,
    source_description: str,
    attestation: str,
) -> ImportBatch:
    preview = preview_real_portfolio(
        db,
        filename,
        raw,
        statement_date=statement_date,
        source_description=source_description,
        attestation=attestation,
    )
    if not preview["activation_allowed"]:
        raise ValueError("Portfolio preview failed closed")
    batch = commit_import(db, filename, raw)
    append_audit(
        db,
        actor="operator",
        event_type="real_portfolio.activated",
        entity_type="import_batch",
        entity_id=batch.id,
        new_state={
            "batch_hash": batch.source_hash,
            "statement_date": statement_date,
            "source_description": source_description.strip(),
            "attestation": attestation.strip(),
            "paper_holdings_affected": False,
            "real_order_submission": False,
        },
    )
    db.commit()
    return batch
