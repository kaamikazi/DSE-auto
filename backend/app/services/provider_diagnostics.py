from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from app.data.provider_capabilities import capabilities_for
from app.data.providers.base import MarketDataProvider

SECRET_KEYS = {"token", "password", "secret", "api_key", "authorization", "cookie"}


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if key.lower() in SECRET_KEYS else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


def diagnose_provider(
    provider: MarketDataProvider, symbol: str, output_dir: Path
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    capabilities = capabilities_for(provider.name)
    report: dict[str, Any] = {
        "provider": provider.name,
        "symbol": symbol,
        "checked_at": datetime.now(UTC).isoformat(),
        "capabilities": capabilities.to_dict(),
        "checks": {},
        "timestamp_classification": "unsupported",
    }
    calls: dict[str, Any] = {
        "symbols": provider.get_symbols,
        "quote": lambda: provider.get_quote(symbol).model_dump(mode="json"),
        "history": lambda: [
            bar.model_dump(mode="json")
            for bar in provider.get_history(symbol, date.today() - timedelta(days=30), date.today())
        ],
        "market_summary": provider.get_market_summary,
        "dsex": lambda: [
            bar.model_dump(mode="json")
            for bar in provider.get_index_history(
                "DSEX", date.today() - timedelta(days=30), date.today()
            )
        ],
        "company_info": lambda: provider.get_company_info(symbol),
        "price_sensitive_news": lambda: provider.get_price_sensitive_news(symbol),
    }
    for name, call in calls.items():
        try:
            raw = redact(call())
            report["checks"][name] = {
                "status": "ok",
                "records": len(raw) if isinstance(raw, list) else 1,
                "raw": raw,
            }
            if name == "quote" and isinstance(raw, dict):
                market_ts = raw.get("market_timestamp")
                received = raw.get("received_at")
                report["timestamp_classification"] = (
                    "exchange_time" if market_ts and market_ts != received else "receipt_time_only"
                )
        except Exception as exc:
            report["checks"][name] = {
                "status": "error",
                "error_type": type(exc).__name__,
                "error": str(exc)[:500],
            }
    encoded = json.dumps(report, sort_keys=True, default=str).encode()
    report["report_hash"] = hashlib.sha256(encoded).hexdigest()
    target = output_dir / f"{provider.name}_{symbol}_{date.today().isoformat()}.json"
    target.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    report["report_path"] = str(target)
    return report
