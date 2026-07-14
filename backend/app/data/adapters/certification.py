from __future__ import annotations

import hashlib
import json
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.data.adapters.sdk import DataAdapter
from app.models import ProviderCertification
from app.schemas.market import TimestampProvenance
from app.services.audit import append_audit


def _check(name: str, passed: bool, detail: object) -> dict[str, object]:
    return {"name": name, "passed": passed, "detail": detail}


def certify_adapter(
    db: Session,
    adapter: DataAdapter,
    symbols: list[str],
    output_dir: Path,
    *,
    max_latency_ms: float = 2_000,
    max_quote_age_seconds: float = 30,
) -> ProviderCertification:
    """Run the fail-closed contract required before a licensed feed can be selected."""

    descriptor = adapter.descriptor()
    now = datetime.now(UTC)
    checks: list[dict[str, object]] = []
    checks.append(
        _check(
            "licensed_for_operational_use",
            descriptor.licensing_status == "licensed",
            descriptor.licensing_status,
        )
    )
    required_capabilities = {
        "polling_quotes",
        "historical_data",
        "dsex_index",
        "corporate_actions",
    }
    checks.append(
        _check(
            "required_capabilities",
            required_capabilities.issubset(descriptor.capabilities),
            sorted(set(descriptor.capabilities)),
        )
    )
    checks.append(
        _check(
            "timestamp_provenance",
            descriptor.timestamp_trust == TimestampProvenance.EXCHANGE_VERIFIED,
            descriptor.timestamp_trust.value,
        )
    )
    available_symbols = {item.upper() for item in adapter.get_symbols()}
    requested_symbols = {item.upper() for item in symbols}
    checks.append(
        _check(
            "symbol_coverage",
            requested_symbols.issubset(available_symbols),
            {
                "requested": sorted(requested_symbols),
                "missing": sorted(requested_symbols - available_symbols),
            },
        )
    )

    schema_failures: list[str] = []
    freshness_failures: list[str] = []
    provenance_failures: list[str] = []
    missing_updates: list[str] = []
    latencies: list[float] = []
    quote_timestamps: list[str] = []
    for symbol in sorted(requested_symbols):
        started = time.perf_counter()
        try:
            quote = adapter.get_quote(symbol)
            latencies.append((time.perf_counter() - started) * 1000)
            quote_timestamps.append(quote.market_timestamp.isoformat())
            timestamp = quote.market_timestamp
        except Exception as exc:
            missing_updates.append(f"{symbol}:{type(exc).__name__}")
            continue
        if quote.symbol != symbol:
            schema_failures.append(f"{symbol}:symbol_mismatch")
        if timestamp.tzinfo is None:
            provenance_failures.append(f"{symbol}:naive_timestamp")
            freshness_failures.append(f"{symbol}:timestamp_not_comparable")
        else:
            age = abs((now - timestamp.astimezone(UTC)).total_seconds())
            if age > max_quote_age_seconds or quote.stale:
                freshness_failures.append(f"{symbol}:stale")
        if quote.timestamp_provenance != TimestampProvenance.EXCHANGE_VERIFIED:
            provenance_failures.append(f"{symbol}:untrusted_timestamp")
    checks.append(_check("quote_schema", not schema_failures, schema_failures))
    checks.append(_check("freshness", not freshness_failures, freshness_failures))
    checks.append(_check("missing_updates", not missing_updates, missing_updates))
    checks.append(
        _check("quote_timestamp_provenance", not provenance_failures, provenance_failures)
    )
    checks.append(
        _check(
            "latency",
            bool(latencies) and max(latencies) <= max_latency_ms,
            {"maximum_ms": max(latencies, default=None), "limit_ms": max_latency_ms},
        )
    )

    start = date.today() - timedelta(days=45)
    end = date.today()
    ordering_failures: list[str] = []
    duplicate_failures: list[str] = []
    consistency_failures: list[str] = []
    historical_missing: list[str] = []
    for symbol in sorted(requested_symbols):
        try:
            first = adapter.get_history(symbol, start, end)
            second = adapter.get_history(symbol, start, end)
        except Exception as exc:
            historical_missing.append(f"{symbol}:{type(exc).__name__}")
            continue
        if not first:
            historical_missing.append(f"{symbol}:empty")
            continue
        timestamps = [item.timestamp for item in first]
        if timestamps != sorted(timestamps):
            ordering_failures.append(symbol)
        if len(timestamps) != len(set(timestamps)):
            duplicate_failures.append(symbol)
        if [item.model_dump(mode="json", exclude={"received_at"}) for item in first] != [
            item.model_dump(mode="json", exclude={"received_at"}) for item in second
        ]:
            consistency_failures.append(symbol)
    checks.append(_check("event_ordering", not ordering_failures, ordering_failures))
    checks.append(_check("duplicate_handling", not duplicate_failures, duplicate_failures))
    checks.append(_check("historical_consistency", not consistency_failures, consistency_failures))
    checks.append(_check("historical_coverage", not historical_missing, historical_missing))
    try:
        dsex = adapter.get_index_history("DSEX", start, end)
        dsex_detail: object = {"bars": len(dsex)}
        dsex_passed = bool(dsex)
    except Exception as exc:
        dsex_detail = type(exc).__name__
        dsex_passed = False
    checks.append(_check("dsex_coverage", dsex_passed, dsex_detail))
    try:
        actions = adapter.corporate_actions(next(iter(sorted(requested_symbols)), "GP"))
        actions_detail: object = (
            {"records": len(actions)} if isinstance(actions, list) else type(actions).__name__
        )
        actions_passed = isinstance(actions, list)
    except Exception as exc:
        actions_detail = type(exc).__name__
        actions_passed = False
    checks.append(_check("corporate_actions_contract", actions_passed, actions_detail))
    try:
        health_first = adapter.adapter_health()
        health_second = adapter.adapter_health()
        reconnect_passed = health_first.state == "healthy" and health_second.state == "healthy"
        reconnect_detail: object = [health_first.state, health_second.state]
    except Exception as exc:
        reconnect_passed = False
        reconnect_detail = type(exc).__name__
    checks.append(
        _check(
            "reconnect_behavior",
            reconnect_passed,
            reconnect_detail,
        )
    )
    passed = all(bool(item["passed"]) for item in checks)
    report: dict[str, Any] = {
        "provider_id": descriptor.adapter_id,
        "generated_at": now.isoformat(),
        "status": "passed" if passed else "failed",
        "activation_allowed": passed,
        "checks": checks,
        "quote_timestamps": quote_timestamps,
        "safety": {"paper_only": True, "live_trading_enabled": False},
    }
    canonical = json.dumps(report, sort_keys=True, separators=(",", ":"), default=str)
    integrity_hash = hashlib.sha256(canonical.encode()).hexdigest()
    certification = ProviderCertification(
        provider_id=descriptor.adapter_id,
        status=str(report["status"]),
        report=report,
        integrity_hash=integrity_hash,
    )
    db.add(certification)
    db.flush()
    audit = append_audit(
        db,
        actor="data-governance",
        event_type="provider.certification_completed",
        entity_type="provider_certification",
        entity_id=certification.id,
        new_state={
            "provider_id": descriptor.adapter_id,
            "status": report["status"],
            "integrity_hash": integrity_hash,
        },
    )
    db.commit()
    output_dir.mkdir(parents=True, exist_ok=True)
    report.update(
        {
            "certification_id": certification.id,
            "integrity_hash": integrity_hash,
            "audit_event_id": audit.id,
        }
    )
    output_path = output_dir / f"{descriptor.adapter_id}_{certification.id}.json"
    output_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    return certification


def provider_may_activate(certification: ProviderCertification | None) -> bool:
    return bool(certification and certification.status == "passed")
