from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.brokers.paper import PaperBroker
from app.models import Order, PaperAccount
from app.services.audit import verify_audit_chain
from app.services.portfolio import derive_portfolio
from app.services.portfolio_imports import commit_import, preview_import, reverse_import


def reference_csv(*, credential_column: bool = False) -> bytes:
    header = (
        "occurred_at,transaction_type,symbol,quantity,price,fees,taxes,broker,account_label,notes"
    )
    row = "2026-01-01T10:00:00+06:00,buy,GP,10,250,10,0,manual,reference,attested"
    if credential_column:
        header += ",broker_password"
        row += ",must-not-be-stored"
    return f"{header}\n{row}\n".encode()


def test_reference_import_is_reversible_and_isolated_from_paper(db: Session) -> None:
    db.add(PaperAccount(id=1, cash=Decimal("1000000"), starting_cash=Decimal("1000000")))
    db.commit()
    raw = reference_csv()

    preview = preview_import(db, "reference.csv", raw)
    assert preview["duplicate_batch_id"] is None
    assert preview["errors"] == []
    assert preview["source_untouched"] is True

    batch = commit_import(db, "reference.csv", raw)
    assert batch.source_hash == preview["source_hash"]
    assert batch.row_count == 1
    assert derive_portfolio(db, account_label="reference").holdings[0].quantity == Decimal("10")
    assert derive_portfolio(db, account_label="paper").holdings == []
    assert PaperBroker(db).reconcile()["healthy"] is True
    assert db.scalar(select(func.count()).select_from(Order)) == 0
    assert verify_audit_chain(db)

    with pytest.raises(ValueError, match="duplicates"):
        commit_import(db, "reference-copy.csv", raw)
    reverse_import(db, batch)
    assert batch.status == "reversed"
    assert derive_portfolio(db, account_label="reference").holdings == []
    assert PaperBroker(db).reconcile()["healthy"] is True


def test_reference_import_rejects_credential_columns(db: Session) -> None:
    preview = preview_import(db, "unsafe.csv", reference_csv(credential_column=True))
    assert preview["errors"]
    assert preview["valid_rows"] == []
