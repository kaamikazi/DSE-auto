from datetime import UTC, datetime
from decimal import Decimal

from app.core.database import Base, SessionLocal, engine
from app.models import PaperAccount, RiskState, Transaction

Base.metadata.create_all(engine)
with SessionLocal() as db:
    if db.get(PaperAccount, 1) is None:
        db.add(PaperAccount(id=1, cash=Decimal("1000000"), starting_cash=Decimal("1000000")))
    if db.get(RiskState, 1) is None:
        db.add(RiskState(id=1, state="healthy", reason="Seeded paper environment"))
    if not db.query(Transaction).first():
        db.add(Transaction(occurred_at=datetime.now(UTC), transaction_type="adjustment", symbol="GP",
                           quantity=Decimal("0"), price=Decimal("0"), notes="Non-position demo seed",
                           source_record={"seed": True}))
    db.commit()
print("Seeded paper account and safe defaults; no investment positions created.")

