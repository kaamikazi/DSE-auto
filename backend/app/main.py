from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from decimal import Decimal

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware

from app.api import router
from app.core.config import get_settings
from app.core.database import Base, SessionLocal, engine
from app.core.logging import configure_logging
from app.models import PaperAccount, RiskState
from app.services.audit import append_audit

settings = get_settings()
configure_logging(settings.LOG_LEVEL)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    Base.metadata.create_all(engine)  # Development fallback; production uses Alembic.
    with SessionLocal() as db:
        if db.get(PaperAccount, 1) is None:
            cash = Decimal(str(settings.PAPER_STARTING_CASH_BDT))
            db.add(PaperAccount(id=1, cash=cash, starting_cash=cash))
        if db.get(RiskState, 1) is None:
            db.add(RiskState(id=1, state="healthy", reason="Paper mode startup diagnostics passed"))
        db.flush()
        append_audit(
            db,
            actor="system",
            event_type="application.started",
            entity_type="application",
            new_state={"trading_mode": "paper", "live_enabled": False},
        )
        db.commit()
    logger.info("DSE AutoTrader started in PAPER mode; live execution is unavailable")
    yield


app = FastAPI(title="DSE AutoTrader", version="0.1.0", lifespan=lifespan, docs_url="/api/docs")
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "X-API-Key"],
)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "testserver"])
app.include_router(router)


@app.get("/")
def root() -> dict[str, object]:
    return {
        "name": "DSE AutoTrader",
        "mode": "paper",
        "live_trading_enabled": False,
        "warning": "Research and supervised paper trading only",
    }
