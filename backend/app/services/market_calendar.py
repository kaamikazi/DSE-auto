from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.services.audit import append_audit


@dataclass(frozen=True)
class CalendarDecision:
    allowed: bool
    phase: str
    reason: str
    local_time: str


class DSEMarketCalendar:
    def __init__(self, config_path: Path, holidays_path: Path | None = None) -> None:
        self.config = json.loads(config_path.read_text(encoding="utf-8"))
        self.tz = ZoneInfo(self.config["timezone"])
        self.holidays: set[date] = set()
        if holidays_path and holidays_path.exists():
            with holidays_path.open(encoding="utf-8-sig") as handle:
                for row in csv.DictReader(handle):
                    if row.get("date"):
                        self.holidays.add(date.fromisoformat(row["date"]))

    def decision(self, at: datetime, db: Session | None = None) -> CalendarDecision:
        local = at.astimezone(self.tz)
        phase, reason, allowed = "closed", "outside_configured_period", False
        if self.config.get("emergency_closed"):
            reason = "manual_emergency_closure"
        elif local.weekday() in self.config["weekend_days"]:
            reason = "weekend"
        elif local.date() in self.holidays:
            reason = "holiday"
        else:
            current = local.time().replace(tzinfo=None)
            for name in ("auction", "continuous"):
                period = self.config["periods"][name]
                if (
                    time.fromisoformat(period["open"])
                    <= current
                    < time.fromisoformat(period["close"])
                ):
                    phase, reason, allowed = name, "configured_trading_period", name == "continuous"
                    break
        result = CalendarDecision(allowed, phase, reason, local.isoformat())
        if db is not None:
            append_audit(
                db,
                actor="market_calendar",
                event_type="calendar.decision",
                entity_type="market_calendar",
                new_state=result.__dict__,
            )
            db.commit()
        return result
