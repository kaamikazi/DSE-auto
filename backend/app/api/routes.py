from __future__ import annotations

import csv
import io
from contextlib import suppress
from dataclasses import asdict
from datetime import date
from decimal import Decimal
from typing import Annotated, cast

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.backtesting import run_backtest
from app.brokers import PaperBroker
from app.core.config import Settings, get_settings
from app.core.database import get_db
from app.core.security import require_api_key
from app.data.providers import DataProviderError, create_provider
from app.models import (
    AuditEvent,
    ImportBatch,
    JobExecution,
    Order,
    PaperSession,
    Signal,
    Transaction,
)
from app.risk import RiskEngine
from app.risk.kill_switch import get_state, set_state
from app.schemas.sessions import PaperSessionCreate
from app.schemas.trading import BacktestRequest, OrderProposalCreate, TransactionCreate
from app.services.audit import audit_status, verify_audit_chain
from app.services.backups import backup_database
from app.services.data_validation import compare_quotes
from app.services.orders import approve_order, propose_order
from app.services.paper_sessions import create_session, summary, transition_session
from app.services.portfolio import add_transaction, derive_portfolio
from app.services.portfolio_imports import commit_import, preview_import, reverse_import
from app.services.readiness import evaluate_readiness
from app.services.scheduler import end_of_day_snapshot_job, reconciliation_job
from app.services.shadow_portfolio import compare_shadow_portfolios
from app.services.signals import moving_average_signal

router = APIRouter(prefix="/api/v1")
Db = Annotated[Session, Depends(get_db)]


@router.post("/paper-sessions", dependencies=[Depends(require_api_key)])
def configure_paper_session(payload: PaperSessionCreate, db: Db) -> dict[str, object]:
    try:
        return summary(
            create_session(
                db,
                payload.name,
                payload.approved_universe,
                payload.strategies,
                payload.risk_profile,
                payload.fill_model,
            )
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.get("/paper-sessions")
def list_paper_sessions(db: Db) -> list[dict[str, object]]:
    return [
        summary(item)
        for item in db.scalars(select(PaperSession).order_by(PaperSession.created_at.desc()))
    ]


@router.get("/paper-readiness")
def paper_readiness(
    db: Db,
    symbol: str = "GP",
    operator_acknowledgement: str = "",
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    provider = create_provider(settings.DATA_PRIMARY_PROVIDER, settings.CSV_DATA_DIR)
    return cast(
        dict[str, object],
        evaluate_readiness(db, settings, provider, symbol, operator_acknowledgement),
    )


@router.post("/shadow-comparison", dependencies=[Depends(require_api_key)])
def shadow_comparison(payload: dict[str, list[float]]) -> dict[str, object]:
    try:
        return cast(dict[str, object], compare_shadow_portfolios(payload))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/operations/backup", dependencies=[Depends(require_api_key)])
def operations_backup(db: Db, settings: Settings = Depends(get_settings)) -> dict[str, object]:
    return cast(dict[str, object], backup_database(db, settings))


@router.post("/operations/end-of-day", dependencies=[Depends(require_api_key)])
def operations_end_of_day() -> dict[str, object]:
    reconciliation_job()
    end_of_day_snapshot_job()
    return {"status": "completed", "paper_only": True}


@router.post("/paper-sessions/{session_id}/{action}", dependencies=[Depends(require_api_key)])
def change_paper_session(
    session_id: str,
    action: str,
    db: Db,
    operator_acknowledgement: str = "",
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    session = db.get(PaperSession, session_id)
    if session is None:
        raise HTTPException(404, "Paper session not found")
    targets = {
        "start": "warming_up",
        "activate": "running",
        "pause": "paused",
        "resume": "running",
        "stop": "stopped",
        "complete": "completed",
    }
    if action not in targets:
        raise HTTPException(422, "Unknown session action")
    try:
        if action in {"start", "activate", "resume"}:
            provider = create_provider(settings.DATA_PRIMARY_PROVIDER, settings.CSV_DATA_DIR)
            gate = evaluate_readiness(
                db,
                settings,
                provider,
                session.approved_universe[0],
                operator_acknowledgement,
            )
            if not gate["ready"]:
                raise ValueError(f"Paper-session readiness gate failed: {gate['checks']}")
        return summary(transition_session(db, session, targets[action], f"operator_{action}"))
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/portfolio/import-preview", dependencies=[Depends(require_api_key)])
async def portfolio_import_preview(db: Db, file: UploadFile = File(...)) -> dict[str, object]:
    return cast(
        dict[str, object], preview_import(db, file.filename or "upload.csv", await file.read())
    )


@router.post("/portfolio/import-commit", dependencies=[Depends(require_api_key)])
async def portfolio_import_commit(db: Db, file: UploadFile = File(...)) -> dict[str, object]:
    try:
        batch = commit_import(db, file.filename or "upload.csv", await file.read())
        return {"batch_id": batch.id, "status": batch.status, "rows": batch.row_count}
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/portfolio/imports/{batch_id}/reverse", dependencies=[Depends(require_api_key)])
def portfolio_import_reverse(batch_id: str, db: Db) -> dict[str, object]:
    batch = db.get(ImportBatch, batch_id)
    if batch is None:
        raise HTTPException(404, "Import batch not found")
    reverse_import(db, batch)
    return {"batch_id": batch.id, "status": batch.status}


def _server_validated_proposal(
    payload: OrderProposalCreate, settings: Settings
) -> OrderProposalCreate:
    """Replace all client-asserted market facts with provider-derived values."""
    primary = create_provider(settings.DATA_PRIMARY_PROVIDER, settings.CSV_DATA_DIR)
    secondary = create_provider(settings.DATA_SECONDARY_PROVIDER, settings.CSV_DATA_DIR)
    quote = primary.get_quote(payload.symbol)
    try:
        secondary_quote = secondary.get_quote(payload.symbol)
    except DataProviderError:
        secondary_quote = None
    comparison = compare_quotes(
        quote,
        secondary_quote,
        max_disagreement_percent=Decimal(str(settings.DATA_MAX_PROVIDER_DISAGREEMENT_PERCENT)),
        max_staleness_seconds=settings.DATA_MAX_STALENESS_SECONDS,
    )
    return payload.model_copy(
        update={
            "current_price": quote.last_price,
            "data_timestamp": quote.market_timestamp,
            "data_quality_status": "valid" if comparison.safe_for_orders else "unsafe",
            "provider_disagreement_percent": comparison.disagreement_percent,
            "bid": quote.bid,
            "ask": quote.ask,
            "average_daily_volume": payload.average_daily_volume or quote.volume,
        }
    )


@router.get("/health")
def health(db: Db, settings: Settings = Depends(get_settings)) -> dict[str, object]:
    database = True
    try:
        db.execute(text("SELECT 1"))
    except Exception:
        database = False
    primary = create_provider(settings.DATA_PRIMARY_PROVIDER, settings.CSV_DATA_DIR)
    status: dict[str, object] = {
        "application": "healthy" if database else "degraded",
        "database": database,
        "provider": primary.health_check(),
        "trading_mode": settings.TRADING_MODE,
        "live_trading_enabled": False,
        "audit_chain_valid": verify_audit_chain(db),
    }
    return status


@router.get("/market/quote/{symbol}")
def quote(symbol: str, settings: Settings = Depends(get_settings)) -> dict[str, object]:
    primary = create_provider(settings.DATA_PRIMARY_PROVIDER, settings.CSV_DATA_DIR)
    secondary = create_provider(settings.DATA_SECONDARY_PROVIDER, settings.CSV_DATA_DIR)
    try:
        primary_quote = primary.get_quote(symbol)
        try:
            secondary_quote = secondary.get_quote(symbol)
        except DataProviderError:
            secondary_quote = None
        comparison = compare_quotes(
            primary_quote,
            secondary_quote,
            max_disagreement_percent=Decimal(str(settings.DATA_MAX_PROVIDER_DISAGREEMENT_PERCENT)),
            max_staleness_seconds=settings.DATA_MAX_STALENESS_SECONDS,
        )
        return comparison.model_dump(mode="json")
    except DataProviderError as exc:
        raise HTTPException(503, str(exc)) from exc


@router.post("/portfolio/transactions", dependencies=[Depends(require_api_key)])
def create_transaction(payload: TransactionCreate, db: Db) -> dict[str, object]:
    transaction = add_transaction(db, payload)
    return {"id": transaction.id, "status": "recorded", "append_only": True}


@router.post("/portfolio/import-csv", dependencies=[Depends(require_api_key)])
async def import_transactions(db: Db, file: UploadFile = File(...)) -> dict[str, object]:
    raw = (await file.read()).decode("utf-8-sig")
    rows = list(csv.DictReader(io.StringIO(raw)))
    imported: list[str] = []
    try:
        for row in rows:
            payload = TransactionCreate.model_validate(row)
            imported.append(add_transaction(db, payload, source_record=row).id)
    except Exception as exc:
        raise HTTPException(422, f"Import stopped at row {len(imported) + 1}: {exc}") from exc
    return {"imported": len(imported), "transaction_ids": imported}


@router.get("/portfolio")
def portfolio(db: Db, settings: Settings = Depends(get_settings)) -> dict[str, object]:
    provider = create_provider(settings.DATA_PRIMARY_PROVIDER, settings.CSV_DATA_DIR)
    symbols = sorted(set(db.scalars(select(Transaction.symbol)).all()))
    prices: dict[str, Decimal] = {}
    for symbol in symbols:
        with suppress(DataProviderError):
            prices[symbol] = provider.get_quote(symbol).last_price
    return derive_portfolio(db, prices).model_dump(mode="json")


@router.post("/backtests")
def backtest(
    payload: BacktestRequest, settings: Settings = Depends(get_settings)
) -> dict[str, object]:
    provider = create_provider(settings.DATA_PRIMARY_PROVIDER, settings.CSV_DATA_DIR)
    end = date.today()
    start = date(end.year - 2, end.month, min(end.day, 28))
    try:
        bars = provider.get_history(payload.symbol, start, end)
        benchmark = provider.get_index_history("DSEX", start, end)
        return cast(dict[str, object], asdict(run_backtest(bars, payload, benchmark)))
    except (DataProviderError, ValueError) as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/signals/{symbol}", dependencies=[Depends(require_api_key)])
def generate_signal(
    symbol: str, db: Db, settings: Settings = Depends(get_settings)
) -> dict[str, object]:
    provider = create_provider(settings.DATA_PRIMARY_PROVIDER, settings.CSV_DATA_DIR)
    end = date.today()
    start = date(end.year - 1, end.month, min(end.day, 28))
    try:
        signal = moving_average_signal(
            db, symbol, provider.get_history(symbol, start, end), provider.get_quote(symbol)
        )
        return {
            "id": signal.id,
            "signal_type": signal.signal_type,
            "strength_score": signal.strength_score,
            "label": "strategy-strength score; not a probability",
        }
    except (DataProviderError, ValueError) as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/signals")
def list_signals(db: Db) -> list[dict[str, object]]:
    return [
        {
            "id": item.id,
            "symbol": item.symbol,
            "signal_type": item.signal_type,
            "strength_score": item.strength_score,
            "timestamp": item.timestamp.isoformat(),
            "data_quality_status": item.data_quality_status,
        }
        for item in db.scalars(select(Signal).order_by(Signal.timestamp.desc()).limit(100))
    ]


@router.post("/orders/proposals", dependencies=[Depends(require_api_key)])
def create_proposal(
    payload: OrderProposalCreate, db: Db, settings: Settings = Depends(get_settings)
) -> dict[str, object]:
    try:
        payload = _server_validated_proposal(payload, settings)
        provider = create_provider(settings.DATA_PRIMARY_PROVIDER, settings.CSV_DATA_DIR)
        order, decision = propose_order(
            db, payload, RiskEngine(), settings.DATA_MAX_STALENESS_SECONDS, provider
        )
        return {
            "order_id": order.id,
            "status": order.status,
            "risk_decision": decision.model_dump(mode="json"),
            "destination": "paper_only",
        }
    except DataProviderError as exc:
        raise HTTPException(503, f"Server-side market validation failed: {exc}") from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/orders/{order_id}/approve", dependencies=[Depends(require_api_key)])
def approve(
    order_id: str, payload: OrderProposalCreate, db: Db, settings: Settings = Depends(get_settings)
) -> dict[str, object]:
    order = db.get(Order, order_id)
    if not order:
        raise HTTPException(404, "Order not found")
    try:
        payload = _server_validated_proposal(payload, settings)
        provider = create_provider(settings.DATA_PRIMARY_PROVIDER, settings.CSV_DATA_DIR)
        decision = approve_order(
            db, order, payload, RiskEngine(), settings.DATA_MAX_STALENESS_SECONDS, provider
        )
        return {
            "order_id": order.id,
            "status": order.status,
            "risk_decision": decision.model_dump(mode="json"),
            "destination": "paper_only",
        }
    except DataProviderError as exc:
        raise HTTPException(503, f"Server-side market revalidation failed: {exc}") from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/orders/{order_id}/execute", dependencies=[Depends(require_api_key)])
def execute(
    order_id: str, market_price: Decimal, available_volume: int, db: Db
) -> dict[str, object]:
    order = db.get(Order, order_id)
    if not order:
        raise HTTPException(404, "Order not found")
    try:
        order = PaperBroker(db).submit_order(order, market_price, available_volume)
        return {
            "order_id": order.id,
            "status": order.status,
            "filled_quantity": order.filled_quantity,
            "average_fill_price": str(order.average_fill_price)
            if order.average_fill_price
            else None,
        }
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/orders/{order_id}/cancel", dependencies=[Depends(require_api_key)])
def cancel(order_id: str, db: Db) -> dict[str, object]:
    order = db.get(Order, order_id)
    if not order:
        raise HTTPException(404, "Order not found")
    try:
        PaperBroker(db).cancel_order(order)
        return {"order_id": order.id, "status": order.status}
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.get("/orders")
def list_orders(db: Db) -> list[dict[str, object]]:
    return [
        {
            "id": item.id,
            "symbol": item.symbol,
            "side": item.side,
            "quantity": item.quantity,
            "limit_price": str(item.limit_price) if item.limit_price else None,
            "status": item.status,
            "filled_quantity": item.filled_quantity,
        }
        for item in db.scalars(select(Order).order_by(Order.created_at.desc()).limit(100))
    ]


@router.get("/risk")
def risk_status(db: Db) -> dict[str, object]:
    state = get_state(db)
    return {
        "state": state.state,
        "reason": state.reason,
        "updated_at": state.updated_at.isoformat(),
    }


@router.post("/risk/emergency-stop", dependencies=[Depends(require_api_key)])
def emergency_stop(db: Db) -> dict[str, object]:
    state = set_state(db, "emergency_stop", "Manual emergency stop", "user")
    return {"state": state.state, "reason": state.reason}


@router.post("/risk/pause", dependencies=[Depends(require_api_key)])
def pause(db: Db) -> dict[str, object]:
    state = set_state(db, "trading_paused", "Manual pause", "user")
    return {"state": state.state, "reason": state.reason}


@router.post("/risk/resume", dependencies=[Depends(require_api_key)])
def resume(db: Db) -> dict[str, object]:
    broker_status = PaperBroker(db).reconcile()
    if not broker_status["healthy"] or not verify_audit_chain(db):
        state = set_state(
            db, "reconciliation_required", "Reconciliation or audit verification failed", "user"
        )
    else:
        state = set_state(
            db, "healthy", "Manual resume after successful paper reconciliation", "user"
        )
    return {"state": state.state, "reason": state.reason, "reconciliation": broker_status}


@router.get("/audit")
def audit_events(db: Db) -> dict[str, object]:
    events = db.scalars(select(AuditEvent).order_by(AuditEvent.timestamp.desc()).limit(200)).all()
    return {
        "chain_valid": verify_audit_chain(db),
        "status": audit_status(db),
        "events": [
            {
                "id": event.id,
                "timestamp": event.timestamp.isoformat(),
                "actor": event.actor,
                "event_type": event.event_type,
                "entity_type": event.entity_type,
                "entity_id": event.entity_id,
                "integrity_hash": event.integrity_hash,
            }
            for event in events
        ],
    }


@router.get("/scheduler/runs")
def scheduler_runs(db: Db) -> list[dict[str, object]]:
    runs = db.scalars(select(JobExecution).order_by(JobExecution.started_at.desc()).limit(50)).all()
    return [
        {
            "id": r.id,
            "job_name": r.job_name,
            "started_at": r.started_at.isoformat(),
            "finished_at": r.finished_at.isoformat() if r.finished_at else None,
            "status": r.status,
            "error_message": r.error_message,
            "attempts": r.attempts,
        }
        for r in runs
    ]


@router.get("/scheduler/health")
def scheduler_health(db: Db) -> dict[str, object]:
    from sqlalchemy import func

    subq = (
        select(
            JobExecution.job_name,
            func.max(JobExecution.started_at).label("max_started"),
        )
        .group_by(JobExecution.job_name)
        .subquery()
    )
    last_runs = db.scalars(
        select(JobExecution).join(
            subq,
            (JobExecution.job_name == subq.c.job_name)
            & (JobExecution.started_at == subq.c.max_started),
        )
    ).all()
    return {
        "healthy": not any(r.status == "failed" for r in last_runs),
        "jobs": {
            r.job_name: {
                "status": r.status,
                "started_at": r.started_at.isoformat(),
                "finished_at": r.finished_at.isoformat() if r.finished_at else None,
                "error_message": r.error_message,
            }
            for r in last_runs
        },
    }
