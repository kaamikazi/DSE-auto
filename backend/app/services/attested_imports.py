from __future__ import annotations

import csv
import hashlib
import io
import re
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models import ImportBatch, MarketBar, ValidationCampaign
from app.services.audit import append_audit
from app.services.events import emit_event

ATTESTATION = (
    "I confirm this file represents the stated DSE market date and source, and I understand "
    "it is operator-attested rather than exchange-verified."
)
BAR_IMPORT_KINDS = {"quote", "ohlcv", "dsex"}
REFERENCE_IMPORT_KINDS = {
    "corporate_action",
    "news",
    "suspension",
    "trading_status",
}
IMPORT_KINDS = BAR_IMPORT_KINDS | REFERENCE_IMPORT_KINDS
SYMBOL_PATTERN = re.compile(r"^[A-Z0-9.-]{1,32}$")
TEMPLATES = {
    "quote": "symbol,timestamp,last_price,volume,source\nGP,2026-07-13T10:30:00+06:00,250.00,10000,operator_file\n",
    "ohlcv": "symbol,timestamp,open,high,low,close,volume,source\nGP,2026-07-13T14:30:00+06:00,248.00,252.00,247.00,250.00,10000,operator_file\n",
    "dsex": "timestamp,index_value,volume,source\n2026-07-13T14:30:00+06:00,5200.00,1000000,operator_file\n",
    "corporate_action": "symbol,timestamp,action_type,details,source\nGP,2026-07-13T14:30:00+06:00,dividend,Reviewed dividend reference,operator_file\n",
    "news": "symbol,timestamp,reference,title,source\nGP,2026-07-13T14:30:00+06:00,https://example.invalid/reference,Reviewed price-sensitive news,operator_file\n",
    "suspension": "symbol,timestamp,status,reason,source\nGP,2026-07-13T14:30:00+06:00,active,Reviewed suspension notice,operator_file\n",
    "trading_status": "symbol,timestamp,status,reason,source\nGP,2026-07-13T14:30:00+06:00,open,Reviewed trading-status notice,operator_file\n",
}


def _parse_timestamp(value: str, market_date: date) -> datetime:
    timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("timestamp must include a trustworthy UTC offset")
    if timestamp.date() != market_date:
        raise ValueError("timestamp does not match the attested market date")
    return timestamp


def _parse_rows(
    raw: bytes,
    import_kind: str,
    market_date: date,
    approved_symbols: set[str] | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        return [], [{"row": 0, "error": f"CSV must be UTF-8: {exc}"}]
    rows = list(csv.DictReader(io.StringIO(text)))
    valid: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for number, raw_row in enumerate(rows, 2):
        row = {key.strip(): (value or "").strip() for key, value in raw_row.items() if key}
        try:
            timestamp = _parse_timestamp(row["timestamp"], market_date)
            symbol = "DSEX" if import_kind == "dsex" else row["symbol"].upper()
            if not SYMBOL_PATTERN.fullmatch(symbol):
                raise ValueError("invalid DSE symbol")
            if approved_symbols is not None and symbol not in approved_symbols and symbol != "DSEX":
                raise ValueError("symbol is outside the campaign-approved universe")
            source = row.get("source", "")
            if not source:
                raise ValueError("source is required")
            if import_kind in REFERENCE_IMPORT_KINDS:
                required = {
                    "corporate_action": ("action_type", "details"),
                    "news": ("reference", "title"),
                    "suspension": ("status", "reason"),
                    "trading_status": ("status", "reason"),
                }[import_kind]
                if any(not row.get(field) for field in required):
                    raise ValueError(f"{import_kind} requires {', '.join(required)}")
                valid.append(
                    {
                        "symbol": symbol,
                        "timestamp": timestamp,
                        "source": source,
                        "record_type": import_kind,
                        "payload": {field: row[field] for field in required},
                    }
                )
                continue
            if import_kind == "quote":
                close = Decimal(row["last_price"])
                open_price = high = low = close
            elif import_kind == "ohlcv":
                open_price = Decimal(row["open"])
                high = Decimal(row["high"])
                low = Decimal(row["low"])
                close = Decimal(row["close"])
                if high < low or not low <= open_price <= high or not low <= close <= high:
                    raise ValueError("OHLC values are inconsistent")
            else:
                close = Decimal(row["index_value"])
                open_price = high = low = close
            if min(open_price, high, low, close) < 0:
                raise ValueError("prices and index values must be non-negative")
            volume_text = row.get("volume", "")
            volume = int(volume_text) if volume_text else None
            if volume is not None and volume < 0:
                raise ValueError("volume must be non-negative")
            valid.append(
                {
                    "symbol": symbol,
                    "timestamp": timestamp,
                    "open": open_price,
                    "high": high,
                    "low": low,
                    "close": close,
                    "volume": volume,
                    "source": source,
                }
            )
        except (KeyError, ValueError, ArithmeticError) as exc:
            errors.append({"row": number, "error": str(exc)})
    if not rows:
        errors.append({"row": 0, "error": "CSV contains no data rows"})
    return valid, errors


def preview_attested_import(
    db: Session,
    *,
    filename: str,
    raw: bytes,
    import_kind: str,
    market_date: date,
    operator_attestation: str,
    raw_dir: Path,
    campaign_id: str | None = None,
) -> dict[str, Any]:
    if import_kind not in IMPORT_KINDS:
        raise ValueError(f"Import kind must be one of: {', '.join(sorted(IMPORT_KINDS))}")
    if operator_attestation.strip() != ATTESTATION:
        raise ValueError(f"Operator must confirm exactly: {ATTESTATION}")
    digest = hashlib.sha256(raw).hexdigest()
    duplicate = db.scalar(select(ImportBatch).where(ImportBatch.source_hash == digest))
    if duplicate:
        raise ValueError(f"Duplicate batch: {duplicate.id}")
    approved: set[str] | None = None
    if campaign_id:
        campaign = db.get(ValidationCampaign, campaign_id)
        if campaign is None:
            raise ValueError("Campaign not found")
        approved = set(campaign.approved_symbols)
    valid, errors = _parse_rows(raw, import_kind, market_date, approved)
    retained = raw_dir / digest[:2] / digest / Path(filename).name
    retained.parent.mkdir(parents=True, exist_ok=True)
    if retained.exists() and retained.read_bytes() != raw:
        raise ValueError("Raw retention collision detected")
    if not retained.exists():
        retained.write_bytes(raw)
    batch = ImportBatch(
        source_name=Path(filename).name,
        source_hash=digest,
        status="previewed" if not errors else "rejected",
        row_count=len(valid) + len(errors),
        errors=errors,
        import_kind=import_kind,
        market_date=market_date,
        operator_attestation=operator_attestation.strip(),
        raw_file_path=str(retained),
        campaign_id=campaign_id,
    )
    db.add(batch)
    db.flush()
    append_audit(
        db,
        actor="operator",
        event_type="data_import.previewed",
        entity_type="import_batch",
        entity_id=batch.id,
        new_state={
            "kind": import_kind,
            "market_date": market_date.isoformat(),
            "hash": digest,
            "valid_rows": len(valid),
            "errors": len(errors),
            "timestamp_provenance": "operator_attested",
        },
    )
    db.commit()
    return {
        "batch_id": batch.id,
        "status": batch.status,
        "source_hash": digest,
        "raw_file_path": str(retained),
        "valid_rows": [
            {
                **row,
                "timestamp": row["timestamp"].isoformat(),
                "timestamp_provenance": "operator_attested",
            }
            for row in valid
        ],
        "errors": errors,
        "activation_allowed": not errors,
        "exchange_verified": False,
    }


def activate_attested_import(
    db: Session,
    batch: ImportBatch,
    activation_approval: str,
) -> ImportBatch:
    if batch.status != "previewed" or batch.errors:
        raise ValueError("Only a validated preview may be activated")
    if len(activation_approval.strip()) < 12:
        raise ValueError("Activation requires an explicit operator approval")
    if batch.raw_file_path is None or batch.market_date is None:
        raise ValueError("Import batch has no retained raw file or market date")
    raw = Path(batch.raw_file_path).read_bytes()
    approved: set[str] | None = None
    if batch.campaign_id:
        campaign = db.get(ValidationCampaign, batch.campaign_id)
        if campaign is None:
            raise ValueError("Campaign not found")
        approved = set(campaign.approved_symbols)
    rows, errors = _parse_rows(raw, batch.import_kind, batch.market_date, approved)
    if errors or hashlib.sha256(raw).hexdigest() != batch.source_hash:
        raise ValueError("Retained raw data failed revalidation")
    source = f"attested_csv:{batch.id}"
    if batch.import_kind in BAR_IMPORT_KINDS:
        for row in rows:
            db.add(
                MarketBar(
                    timestamp=row["timestamp"],
                    symbol=row["symbol"],
                    open=row["open"],
                    high=row["high"],
                    low=row["low"],
                    close=row["close"],
                    volume=row["volume"],
                    source=source,
                    quality_status="valid",
                    timestamp_provenance="operator_attested",
                    import_batch_id=batch.id,
                    campaign_id=batch.campaign_id,
                )
            )
    batch.status = "activated"
    batch.activated_at = datetime.now(UTC)
    emit_event(
        db,
        "data_activated",
        aggregate_type="import_batch",
        aggregate_id=batch.id,
        payload={
            "rows": len(rows),
            "campaign_id": batch.campaign_id,
            "timestamp_provenance": "operator_attested",
        },
        idempotency_key=f"data-activated:{batch.id}",
        correlation_id=batch.campaign_id,
    )
    append_audit(
        db,
        actor="operator",
        event_type="data_import.activated",
        entity_type="import_batch",
        entity_id=batch.id,
        new_state={
            "rows": len(rows),
            "campaign_id": batch.campaign_id,
            "approval": activation_approval.strip(),
        },
        metadata={"timestamp_provenance": "operator_attested", "exchange_verified": False},
    )
    db.commit()
    return batch


def rollback_attested_import(db: Session, batch: ImportBatch, reason: str) -> ImportBatch:
    if batch.status != "activated":
        raise ValueError("Only an activated import may be rolled back")
    if len(reason.strip()) < 8:
        raise ValueError("Rollback reason is required")
    delete_result = db.execute(delete(MarketBar).where(MarketBar.import_batch_id == batch.id))
    removed = int(getattr(delete_result, "rowcount", 0))
    batch.status = "rolled_back"
    batch.reversed_at = datetime.now(UTC)
    append_audit(
        db,
        actor="operator",
        event_type="data_import.rolled_back",
        entity_type="import_batch",
        entity_id=batch.id,
        new_state={"removed_rows": removed, "reason": reason.strip(), "raw_retained": True},
    )
    db.commit()
    return batch


def import_template(import_kind: str) -> str:
    try:
        return TEMPLATES[import_kind]
    except KeyError as exc:
        raise ValueError(f"Import kind must be one of: {', '.join(sorted(IMPORT_KINDS))}") from exc
