from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.brokers.paper import PaperBroker
from app.core.config import Settings
from app.data.providers.base import MarketDataProvider
from app.models import JobExecution, RiskState
from app.schemas.market import TimestampProvenance
from app.services.audit import audit_status
from app.services.market_calendar import DSEMarketCalendar


def evaluate_readiness(
    db: Session,
    settings: Settings,
    provider: MarketDataProvider,
    symbol: str,
    operator_acknowledgement: str = "",
) -> dict[str, Any]:
    checks: dict[str, dict[str, Any]] = {}
    audit = audit_status(db)
    checks["audit"] = {"passed": audit["canonical_valid"], "detail": audit}
    health = provider.health_check()
    checks["data_source"] = {"passed": bool(health.get("healthy")), "detail": health}
    try:
        quote = provider.get_quote(symbol)
        trusted = quote.timestamp_provenance in {
            TimestampProvenance.EXCHANGE_VERIFIED,
            TimestampProvenance.OPERATOR_ATTESTED,
        }
        checks["timestamp_trust"] = {
            "passed": trusted,
            "provenance": quote.timestamp_provenance,
            "market_timestamp": quote.market_timestamp.isoformat(),
        }
    except Exception as exc:
        checks["timestamp_trust"] = {"passed": False, "error": str(exc)}
    reconciliation = PaperBroker(db).reconcile()
    checks["paper_account"] = {"passed": bool(reconciliation["healthy"]), "detail": reconciliation}
    try:
        DSEMarketCalendar(settings.MARKET_CALENDAR_PATH, settings.DSE_HOLIDAYS_PATH)
        checks["market_calendar"] = {"passed": True, "path": str(settings.MARKET_CALENDAR_PATH)}
    except Exception as exc:
        checks["market_calendar"] = {"passed": False, "error": str(exc)}
    cutoff = datetime.now(UTC) - timedelta(hours=24)
    recent = db.scalar(
        select(JobExecution)
        .where(JobExecution.started_at >= cutoff)
        .order_by(JobExecution.started_at.desc())
    )
    checks["scheduler"] = {
        "passed": recent is not None and recent.status != "failed",
        "last_status": recent.status if recent else None,
    }
    risk = db.get(RiskState, 1)
    checks["emergency_stop"] = {
        "passed": risk is not None
        and risk.state not in {"emergency_stop", "reconciliation_required"},
        "state": risk.state if risk else "missing",
    }
    backups = list((Path("../data/backups")).glob("dse_autotrader_backup_*.db"))
    checks["backup"] = {
        "passed": bool(backups),
        "latest": str(max(backups, key=lambda path: path.stat().st_mtime)) if backups else None,
    }
    checks["operator_acknowledgement"] = {"passed": len(operator_acknowledgement.strip()) >= 12}
    ready = all(bool(check["passed"]) for check in checks.values())
    return {
        "ready": ready,
        "evaluated_at": datetime.now(UTC).isoformat(),
        "symbol": symbol.upper(),
        "checks": checks,
    }
