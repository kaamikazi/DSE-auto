from __future__ import annotations

import csv
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.brokers.paper import PaperBroker
from app.core.config import Settings
from app.data.providers.csv_provider import OperatorAttestedCSVProvider
from app.models import JobExecution, PaperSessionRun
from app.risk.engine import RiskEngine, RiskLimits
from app.schemas.trading import OrderProposalCreate
from app.services.audit import append_audit, verify_audit_chain
from app.services.evidence import generate_evidence_pack
from app.services.orders import approve_order, propose_order
from app.services.paper_sessions import create_session, transition_session
from app.services.readiness import evaluate_readiness
from app.services.shadow_portfolio import compare_shadow_portfolios
from app.services.signals import moving_average_signal


def _write_attested_fixture(root: Path, symbol: str, now: datetime) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{symbol}.csv"
    with path.open("w", newline="", encoding="utf-8") as handle:
        fields = [
            "timestamp",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "trade_count",
            "turnover",
            "timestamp_provenance",
        ]
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for index in range(80):
            price = Decimal("100") + Decimal(index) / 10
            writer.writerow(
                {
                    "timestamp": (now - timedelta(days=79 - index)).isoformat(),
                    "open": price,
                    "high": price + 1,
                    "low": price - 1,
                    "close": price + Decimal("0.2"),
                    "volume": 100000 + index * 100,
                    "trade_count": 500,
                    "turnover": price * 100000,
                    "timestamp_provenance": "operator_attested",
                }
            )
    return path


def run_complete_imported_session(
    db: Session, settings: Settings, output_root: Path, operator_acknowledgement: str
) -> dict[str, Any]:
    if not verify_audit_chain(db):
        raise ValueError("Canonical audit chain must be valid before a paper session")
    now = datetime.now(UTC)
    import_root = output_root / "imports"
    _write_attested_fixture(import_root, "GP", now)
    provider = OperatorAttestedCSVProvider(import_root)
    db.add(
        JobExecution(
            job_name="imported_session_manual", status="success", started_at=now, finished_at=now
        )
    )
    db.commit()
    gate = evaluate_readiness(db, settings, provider, "GP", operator_acknowledgement)
    if not gate["ready"]:
        raise ValueError(f"Readiness gate failed: {gate['checks']}")
    session = create_session(
        db,
        f"imported-validation-{now.strftime('%Y%m%dT%H%M%S')}",
        ["GP"],
        ["ma_crossover"],
        {"profile": "conservative"},
        "pessimistic",
    )
    transition_session(db, session, "warming_up", "readiness_gate_passed", "operator")
    transition_session(db, session, "running", "imported_data_session_started", "operator")
    quote = provider.get_quote("GP")
    append_audit(
        db,
        actor="imported_session",
        event_type="quote.recorded",
        entity_type="quote",
        entity_id="GP",
        new_state=quote.model_dump(mode="json"),
    )
    db.commit()
    bars = provider.get_history("GP", (now - timedelta(days=100)).date(), now.date())
    signal = moving_average_signal(db, "GP", bars, quote)
    proposal = OrderProposalCreate(
        idempotency_key=f"m5-{session.id}",
        symbol="GP",
        side="buy",
        quantity=10,
        limit_price=quote.last_price,
        current_price=quote.last_price,
        strategy_id=signal.strategy_id,
        data_timestamp=quote.market_timestamp,
        data_quality_status="valid",
        average_daily_volume=quote.volume,
    )
    order, decision = propose_order(
        db,
        proposal,
        RiskEngine(RiskLimits(approved_symbols=("GP",))),
        settings.DATA_MAX_STALENESS_SECONDS,
        provider,
    )
    if not decision.approved:
        raise ValueError(f"Imported session proposal rejected: {decision.reason_codes}")
    approval = approve_order(
        db,
        order,
        proposal,
        RiskEngine(RiskLimits(approved_symbols=("GP",))),
        settings.DATA_MAX_STALENESS_SECONDS,
        provider,
    )
    if not approval.approved:
        raise ValueError(f"Imported session approval rejected: {approval.reason_codes}")
    PaperBroker(db, fill_model="pessimistic").submit_order(
        order, quote.last_price, quote.volume or 0
    )
    reconciliation = PaperBroker(db).reconcile()
    db.add(
        PaperSessionRun(
            session_id=session.id,
            run_type="end_of_day",
            status="success" if reconciliation["healthy"] else "failed",
            metrics=reconciliation,
            finished_at=datetime.now(UTC),
        )
    )
    append_audit(
        db,
        actor="operator",
        event_type="paper_session.end_of_day",
        entity_type="paper_session",
        entity_id=session.id,
        new_state=reconciliation,
    )
    db.commit()
    if not reconciliation["healthy"]:
        transition_session(
            db, session, "reconciliation_required", "end_of_day_mismatch", "operator"
        )
        raise ValueError("End-of-day reconciliation failed")
    evidence = generate_evidence_pack(
        session.name,
        "ma_crossover-v1",
        [bar.model_dump(mode="json") for bar in bars],
        [
            {
                "order_id": order.id,
                "symbol": order.symbol,
                "quantity": order.filled_quantity,
                "price": str(order.average_fill_price),
                "status": order.status,
            }
        ],
        {"validation": {"session": "imported_data"}, "failure_cases": []},
        output_root / "evidence",
        provider=provider.name,
        fill_model=session.fill_model,
    )
    shadow = compare_shadow_portfolios(
        {
            "reference_imported": [100, 100.5, 101],
            "buy_and_hold": [100, 101, 101.5],
            "dsex": [100, 99.8, 100.2],
            "strategy_ma": [100, 100.4, 100.8],
            "combined_paper": [100, 100.4, 100.8],
        }
    )
    transition_session(db, session, "completed", "end_of_day_reconciled", "operator")
    return {
        "session_id": session.id,
        "session_name": session.name,
        "state": session.state,
        "readiness": gate,
        "quote_provenance": quote.timestamp_provenance,
        "signal_id": signal.id,
        "order_id": order.id,
        "order_status": order.status,
        "reconciliation": reconciliation,
        "evidence": evidence,
        "shadow_comparison": shadow,
        "audit_valid": verify_audit_chain(db),
    }
