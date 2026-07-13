from __future__ import annotations

import csv
import hashlib
import json
from collections import Counter
from datetime import UTC, date, datetime
from pathlib import Path
from statistics import mean
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import DataQualityObservation, DataQualityReport
from app.schemas.market import HistoricalBar, Quote, TimestampProvenance

TRUSTWORTHY_FOR_APPROVAL = {
    TimestampProvenance.EXCHANGE_VERIFIED,
    TimestampProvenance.PROVIDER_ASSERTED,
    TimestampProvenance.OPERATOR_ATTESTED,
}


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def measure_data_quality(
    quotes: list[Quote],
    *,
    expected_symbols: set[str],
    now: datetime | None = None,
    activated_at: datetime | None = None,
    secondary_prices: dict[str, float] | None = None,
    max_quote_age_seconds: int = 30,
    expected_updates: int | None = None,
) -> dict[str, Any]:
    current = now or datetime.now(UTC)
    activated = activated_at or current
    identities = [(item.symbol, _aware(item.market_timestamp).isoformat()) for item in quotes]
    counts = Counter(identities)
    duplicate_count = sum(count - 1 for count in counts.values() if count > 1)
    quote_ages = [
        max((current - _aware(item.market_timestamp)).total_seconds(), 0) for item in quotes
    ]
    source_latencies = [
        max((_aware(item.received_at) - _aware(item.market_timestamp)).total_seconds(), 0)
        for item in quotes
    ]
    ingestion_latencies = [
        max((current - _aware(item.received_at)).total_seconds(), 0) for item in quotes
    ]
    activation_latencies = [
        max((_aware(activated) - _aware(item.received_at)).total_seconds(), 0) for item in quotes
    ]
    out_of_order = sum(
        _aware(quotes[index].market_timestamp) < _aware(quotes[index - 1].market_timestamp)
        for index in range(1, len(quotes))
        if quotes[index].symbol == quotes[index - 1].symbol
    )
    present = {quote.symbol for quote in quotes}
    missing_symbols = sorted(expected_symbols - present)
    expected_count = expected_updates if expected_updates is not None else len(expected_symbols)
    missing_updates = max(expected_count - len(identities), 0)
    disagreements: list[float] = []
    for quote in quotes:
        secondary = (secondary_prices or {}).get(quote.symbol)
        if secondary is not None and secondary != 0:
            disagreements.append(abs(float(quote.last_price) - secondary) / secondary * 100)
    stale_flags = [
        age > max_quote_age_seconds or quote.stale
        for age, quote in zip(quote_ages, quotes, strict=True)
    ]
    longest_stale = current_stale = 0
    for stale in stale_flags:
        current_stale = current_stale + 1 if stale else 0
        longest_stale = max(longest_stale, current_stale)
    untrusted = sorted(
        {
            item.timestamp_provenance.value
            for item in quotes
            if item.timestamp_provenance not in TRUSTWORTHY_FOR_APPROVAL
        }
    )
    metrics: dict[str, Any] = {
        "quote_count": len(quotes),
        "quote_age_seconds_mean": mean(quote_ages) if quote_ages else None,
        "quote_age_seconds_max": max(quote_ages) if quote_ages else None,
        "source_latency_seconds_mean": mean(source_latencies) if source_latencies else None,
        "ingestion_latency_seconds_mean": mean(ingestion_latencies)
        if ingestion_latencies
        else None,
        "activation_latency_seconds_mean": mean(activation_latencies)
        if activation_latencies
        else None,
        "duplicate_count": duplicate_count,
        "duplicate_rate": duplicate_count / len(identities) if identities else 0.0,
        "missing_update_count": missing_updates,
        "missing_update_rate": missing_updates / expected_count if expected_count else 0.0,
        "provider_disagreement_percent_max": max(disagreements) if disagreements else None,
        "out_of_order_events": out_of_order,
        "stale_intervals": sum(stale_flags),
        "longest_stale_interval_updates": longest_stale,
        "symbol_coverage": len(present & expected_symbols) / len(expected_symbols)
        if expected_symbols
        else 1.0,
        "missing_symbols": missing_symbols,
        "market_session_coverage": min(len(identities) / expected_count, 1.0)
        if expected_count
        else 1.0,
        "timestamp_trust_levels": sorted({item.timestamp_provenance.value for item in quotes}),
        "untrusted_timestamp_levels": untrusted,
    }
    metrics["passed"] = bool(
        quotes
        and not missing_symbols
        and not untrusted
        and not any(stale_flags)
        and duplicate_count == 0
        and out_of_order == 0
    )
    return metrics


def persist_observations(
    db: Session,
    quotes: list[Quote],
    metrics: dict[str, Any],
    *,
    campaign_id: str | None = None,
) -> list[DataQualityObservation]:
    observations: list[DataQualityObservation] = []
    for quote in quotes:
        quote_age = max((datetime.now(UTC) - _aware(quote.market_timestamp)).total_seconds(), 0)
        observation = DataQualityObservation(
            campaign_id=campaign_id,
            market_date=_aware(quote.market_timestamp).date(),
            symbol=quote.symbol,
            provider=quote.source,
            timestamp_trust=quote.timestamp_provenance.value,
            metrics={
                "quote_age_seconds": quote_age,
                "source_latency_seconds": max(
                    (_aware(quote.received_at) - _aware(quote.market_timestamp)).total_seconds(), 0
                ),
                "batch": metrics,
            },
            passed=bool(metrics["passed"]),
        )
        db.add(observation)
        observations.append(observation)
    db.commit()
    return observations


def _report_svg(metrics: dict[str, Any]) -> str:
    values = [
        ("Coverage", float(metrics.get("symbol_coverage", 0)) * 100),
        ("Session", float(metrics.get("market_session_coverage", 0)) * 100),
        ("Fresh", 100 if not metrics.get("stale_intervals") else 0),
    ]
    bars = "".join(
        f'<text x="10" y="{35 + i * 35}" font-size="12">{label}</text>'
        f'<rect x="90" y="{20 + i * 35}" width="{max(value, 0) * 2}" height="20" fill="#2563eb" />'
        f'<text x="300" y="{35 + i * 35}" font-size="12">{value:.1f}%</text>'
        for i, (label, value) in enumerate(values)
    )
    return f'<svg xmlns="http://www.w3.org/2000/svg" width="360" height="140">{bars}</svg>'


def generate_data_quality_report(
    db: Session,
    *,
    scope: str,
    start_date: date,
    end_date: date,
    campaign_id: str | None,
    output_dir: Path,
) -> DataQualityReport:
    if scope not in {"daily", "weekly", "campaign"}:
        raise ValueError("Data-quality report scope must be daily, weekly, or campaign")
    observations = db.scalars(
        select(DataQualityObservation).where(
            DataQualityObservation.market_date >= start_date,
            DataQualityObservation.market_date <= end_date,
            DataQualityObservation.campaign_id == campaign_id,
        )
    ).all()
    batch_metrics = [item.metrics.get("batch", {}) for item in observations]
    symbols = {item.symbol for item in observations}
    metrics: dict[str, Any] = {
        "observations": len(observations),
        "symbols": sorted(symbols),
        "passing_observations": sum(item.passed for item in observations),
        "symbol_coverage": max(
            (float(item.get("symbol_coverage", 0)) for item in batch_metrics), default=0.0
        ),
        "market_session_coverage": mean(
            [float(item.get("market_session_coverage", 0)) for item in batch_metrics]
        )
        if batch_metrics
        else 0.0,
        "stale_intervals": sum(int(item.get("stale_intervals", 0)) for item in batch_metrics),
        "duplicate_count": sum(int(item.get("duplicate_count", 0)) for item in batch_metrics),
        "out_of_order_events": sum(
            int(item.get("out_of_order_events", 0)) for item in batch_metrics
        ),
        "provider_disagreement_percent_max": max(
            (
                float(item["provider_disagreement_percent_max"])
                for item in batch_metrics
                if item.get("provider_disagreement_percent_max") is not None
            ),
            default=None,
        ),
    }
    metrics["passed"] = bool(observations and all(item.passed for item in observations))
    stem = f"{scope}_{campaign_id or 'global'}_{start_date}_{end_date}"
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path, csv_path, chart_path = (
        output_dir / f"{stem}.json",
        output_dir / f"{stem}.csv",
        output_dir / f"{stem}.svg",
    )
    payload = {
        "scope": scope,
        "campaign_id": campaign_id,
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "metrics": metrics,
        "strategy_results_permitted": bool(metrics["passed"]),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode()).hexdigest()
    json_path.write_text(
        json.dumps(payload | {"integrity_hash": digest}, indent=2), encoding="utf-8"
    )
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["metric", "value"])
        writer.writerows((key, json.dumps(value)) for key, value in sorted(metrics.items()))
    chart_path.write_text(_report_svg(metrics), encoding="utf-8")
    report = db.scalar(
        select(DataQualityReport).where(
            DataQualityReport.scope == scope,
            DataQualityReport.campaign_id == campaign_id,
            DataQualityReport.start_date == start_date,
            DataQualityReport.end_date == end_date,
        )
    )
    if report is None:
        report = DataQualityReport(
            scope=scope,
            campaign_id=campaign_id,
            start_date=start_date,
            end_date=end_date,
            metrics=metrics,
            json_path=str(json_path),
            csv_path=str(csv_path),
            chart_path=str(chart_path),
            integrity_hash=digest,
            passed=bool(metrics["passed"]),
        )
        db.add(report)
    else:
        report.metrics = metrics
        report.json_path = str(json_path)
        report.csv_path = str(csv_path)
        report.chart_path = str(chart_path)
        report.integrity_hash = digest
        report.passed = bool(metrics["passed"])
    db.commit()
    return report


def inline_quality_evidence(rows: list[HistoricalBar]) -> dict[str, Any]:
    identities = [(row.symbol, _aware(row.timestamp).isoformat(), row.source) for row in rows]
    timestamps = [_aware(row.timestamp) for row in rows]
    evidence: dict[str, Any] = {
        "row_count": len(rows),
        "duplicate_rows": len(identities) - len(set(identities)),
        "out_of_order_events": sum(
            timestamps[index] < timestamps[index - 1] for index in range(1, len(timestamps))
        ),
        "unknown_timestamp_rows": sum(
            row.timestamp_provenance == TimestampProvenance.UNKNOWN for row in rows
        ),
        "sources": sorted({row.source for row in rows}),
    }
    evidence["passed"] = bool(
        rows
        and evidence["duplicate_rows"] == 0
        and evidence["out_of_order_events"] == 0
        and evidence["unknown_timestamp_rows"] == 0
    )
    evidence["integrity_hash"] = hashlib.sha256(
        json.dumps(evidence, sort_keys=True).encode()
    ).hexdigest()
    return evidence
