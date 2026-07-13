from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from sqlalchemy.orm import Session

from app.brokers.paper import PaperBroker
from app.core.config import Settings
from app.models import CampaignDay, Order, PaperAccount, PaperSession, RiskState
from app.services.audit import append_audit, verify_audit_chain
from app.services.backups import backup_database
from app.services.campaigns import (
    campaign_summary,
    create_campaign,
    recover_campaigns_after_restart,
    recover_missed_eod,
    transition_campaign,
)
from app.services.governance import (
    create_fee_profile,
    create_rule_set,
    promote_strategy,
    register_strategy,
)
from app.services.incidents import open_incident


def _trading_dates(start: date, count: int) -> list[date]:
    dates: list[date] = []
    current = start
    while len(dates) < count:
        if current.weekday() in {6, 0, 1, 2, 3}:  # Sunday through Thursday
            dates.append(current)
        current += timedelta(days=1)
    return dates


def _rules() -> dict[str, object]:
    return {
        "market_timezone": "Asia/Dhaka",
        "weekly_trading_days": ["sunday", "monday", "tuesday", "wednesday", "thursday"],
        "trading_sessions": [{"start": "10:00", "end": "14:30"}],
        "auction_periods": {"opening": ["09:55", "10:00"], "closing": ["14:30", "14:35"]},
        "holidays": [],
        "tick_sizes": [{"minimum": 0, "tick": 0.1}],
        "price_bands": {"default_percent": 10},
        "settlement_assumptions": {"equity": "T+2", "unverified": True},
        "short_selling_policy": "blocked",
        "leverage_policy": "blocked",
        "minimum_order_quantity": 1,
        "order_expiry_rules": "expire_at_end_of_session",
        "transaction_fee_assumptions": "use_active_fee_profile",
        "tax_assumptions": "conservative",
        "liquidity_thresholds": {"minimum_daily_volume": 1000},
    }


def run_accelerated_campaign(
    db: Session,
    settings: Settings,
    output_dir: Path,
    backup_dir: Path,
) -> dict[str, object]:
    suffix = uuid4().hex[:8]
    dates = _trading_dates(date(2026, 1, 4), 20)
    if db.get(PaperAccount, 1) is None:
        db.add(PaperAccount(id=1, cash=Decimal("1000000"), starting_cash=Decimal("1000000")))
    if db.get(RiskState, 1) is None:
        db.add(RiskState(id=1, state="healthy", reason="Simulation safety initialization"))
    db.commit()
    rule_set = create_rule_set(
        db,
        version=f"simulation-{suffix}",
        effective_date=dates[0],
        source_reference="Operator-reviewed simulation assumptions; not represented as verified DSE rules",
        verification_status="assumed",
        operator_approval="Operator approves paper-only simulation rule assumptions",
        rules=_rules(),
    )
    fee_profile = create_fee_profile(
        db,
        name=f"conservative-simulation-{suffix}",
        version="1.0",
        effective_date=dates[0],
        configuration={},
        broker="paper",
        account_label="simulation",
    )
    references: list[str] = []
    for name in ("moving_average", "mean_reversion"):
        strategy_id = f"{name}_{suffix}"
        registration = register_strategy(
            db,
            strategy_id=strategy_id,
            version="1.0",
            code_hash=hashlib.sha256(f"{strategy_id}:1.0".encode()).hexdigest(),
            parameters={"simulation": True},
            data_requirements={"minimum_bars": 60},
            minimum_sample_size=20,
            evidence={
                "backtest_report": "simulation_fixture",
                "walk_forward_report": "simulation_fixture",
                "sensitivity_report": "simulation_fixture",
                "risk_review": "paper_only",
                "sample_size": 100,
            },
        )
        promote_strategy(db, registration, "research", "Operator research review")
        promote_strategy(db, registration, "paper_candidate", "Operator approves paper candidacy")
        promote_strategy(
            db, registration, "paper_active", "Operator approves simulation activation"
        )
        references.append(f"{strategy_id}@1.0")
    campaign = create_campaign(
        db,
        name=f"20-day-accelerated-{suffix}",
        start_date=dates[0],
        planned_end_date=dates[-1],
        approved_symbols=["GP", "ACI", "BRACBANK"],
        approved_strategies=references,
        starting_capital=Decimal("1000000"),
        risk_profile={"max_drawdown": 0.08, "daily_loss_limit": 0.02},
        data_source_policy={"approved": ["operator_attested"], "fail_closed": True},
        timestamp_trust_requirement="operator_attested",
        fill_model="pessimistic",
        benchmark="DSEX",
        operator_notes="Accelerated deterministic validation; no profitability claim",
        active_rule_set_id=rule_set.id,
        active_fee_profile_id=fee_profile.id,
    )
    transition_campaign(db, campaign, "active", "Start accelerated paper-only verification")
    equity = Decimal("1000000")
    events: list[str] = []
    for index, market_date in enumerate(dates, 1):
        change = Decimal(str(((index % 5) - 2) * 0.0015))
        if index == 15:
            change = Decimal("-0.09")
        equity = (equity * (Decimal("1") + change)).quantize(Decimal("0.01"))
        summary: dict[str, object] = {
            "symbols": ["GP", "ACI", "BRACBANK"],
            "strategies": references,
            "account_snapshot": {"cash": str(equity)},
            "benchmark_value": 100 + index * 0.1,
            "quotes_ingested": 3,
            "timestamp_provenance": "operator_attested",
            "rule_set_id": rule_set.id,
            "fee_profile_id": fee_profile.id,
            "paper_only": True,
        }
        if index == 3:
            open_incident(
                db,
                "provider_outage",
                "high",
                campaign_id=campaign.id,
                evidence={"fallback": "attested_import"},
            )
            summary["data_quality_incidents"] = 1
            summary["missed_trades"] = 2
            events.append("provider_outage")
        if index == 5:
            open_incident(
                db, "stale_data", "high", campaign_id=campaign.id, evidence={"orders_blocked": True}
            )
            summary["data_quality_incidents"] = 1
            summary["missed_trades"] = 1
            events.append("stale_data")
        if index == 7:
            order_time = datetime.combine(market_date, datetime.min.time(), tzinfo=UTC)
            db.add(
                Order(
                    idempotency_key=f"{campaign.id}:partial",
                    symbol="GP",
                    side="buy",
                    order_type="limit",
                    quantity=100,
                    limit_price=Decimal("250"),
                    status="partially_filled",
                    filled_quantity=40,
                    strategy_id=references[0],
                    campaign_id=campaign.id,
                    created_at=order_time,
                    updated_at=order_time,
                )
            )
            summary["partial_fills"] = 1
            events.append("partial_fill")
        if index == 8:
            order_time = datetime.combine(market_date, datetime.min.time(), tzinfo=UTC)
            db.add(
                Order(
                    idempotency_key=f"{campaign.id}:rejected",
                    symbol="ACI",
                    side="buy",
                    order_type="limit",
                    quantity=50,
                    limit_price=Decimal("180"),
                    status="rejected",
                    strategy_id=references[1],
                    campaign_id=campaign.id,
                    created_at=order_time,
                    updated_at=order_time,
                )
            )
            summary["rejected_trades"] = 1
            summary["risk_interventions"] = 1
            events.append("rejected_order")
        now = datetime.now(UTC)
        if index in {10, 12}:
            session = PaperSession(
                name=f"{campaign.name}-{market_date}",
                account_id=1,
                state="running",
                starting_cash=Decimal("1000000"),
                approved_universe=campaign.approved_symbols,
                strategies=campaign.approved_strategies,
                risk_profile=campaign.risk_profile,
                fill_model=campaign.fill_model,
                started_at=now,
                heartbeat_at=now,
                campaign_id=campaign.id,
                market_rule_set_id=campaign.active_rule_set_id,
                fee_profile_id=campaign.active_fee_profile_id,
            )
            db.add(session)
            db.flush()
            day = CampaignDay(
                campaign_id=campaign.id,
                market_date=market_date,
                session_id=session.id,
                state="market_open",
                premarket_completed=True,
                summary=summary,
                started_at=now,
            )
            db.add(day)
            db.commit()
            if index == 12:
                recover_campaigns_after_restart(db, dates[index])
                events.append("restart_recovery")
            recover_missed_eod(
                db,
                campaign,
                settings,
                as_of=dates[index],
                evidence_dir=output_dir,
                backup_dir=backup_dir,
            )
            transition_campaign(db, campaign, "active", "Operator resumes after completed recovery")
            events.append("missed_eod_recovery")
            continue
        day = CampaignDay(
            campaign_id=campaign.id,
            market_date=market_date,
            state="completed",
            premarket_completed=True,
            eod_completed=True,
            summary=summary,
            started_at=now,
            completed_at=now,
        )
        db.add(day)
        append_audit(
            db,
            actor="accelerated_simulation",
            event_type="campaign.day_completed",
            entity_type="campaign_day",
            entity_id=str(day.id),
            new_state={"campaign_id": campaign.id, "market_date": market_date.isoformat()},
        )
        db.commit()
        if index == 15:
            open_incident(
                db,
                "campaign_drawdown_breach",
                "critical",
                campaign_id=campaign.id,
                evidence={"risk_intervention": "campaign_paused_then_reviewed", "drawdown": 0.09},
            )
            summary["risk_interventions"] = 1
            events.append("drawdown_intervention")
    reconciliation = PaperBroker(db).reconcile()
    if not reconciliation["healthy"]:
        raise ValueError("Accelerated campaign final reconciliation failed")
    transition_campaign(db, campaign, "completed", "Twenty trading-day simulation completed")
    report = campaign_summary(db, campaign)
    output_dir.mkdir(parents=True, exist_ok=True)
    report_path = output_dir / f"{campaign.id}_20_day_campaign.json"
    html_path = output_dir / f"{campaign.id}_20_day_campaign.html"
    result: dict[str, object] = {
        "campaign_id": campaign.id,
        "campaign_name": campaign.name,
        "trading_days": 20,
        "symbols": ["GP", "ACI", "BRACBANK"],
        "strategies": references,
        "events": events,
        "final_reconciliation": reconciliation,
        "audit_valid": verify_audit_chain(db),
        "campaign_state": campaign.state,
        "summary": report,
        "profitability_claimed": False,
    }
    report_path.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    html_path.write_text(
        "<!doctype html><title>20-day paper campaign</title>"
        "<h1>Accelerated 20-trading-day PAPER campaign</h1>"
        "<p>LIVE TRADING DISABLED. Results are not a profitability claim.</p>"
        f"<pre>{json.dumps(result, indent=2, default=str)}</pre>",
        encoding="utf-8",
    )
    final_backup = backup_database(db, settings, backup_dir)
    result.update(
        {
            "report_path": str(report_path),
            "html_report_path": str(html_path),
            "final_backup": final_backup,
            "evidence_pack_generated": report_path.exists() and html_path.exists(),
        }
    )
    return result
