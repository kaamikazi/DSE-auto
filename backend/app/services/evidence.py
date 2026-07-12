from __future__ import annotations

import csv
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def generate_evidence_pack(
    name: str,
    strategy_version: str,
    data: list[dict[str, Any]],
    trades: list[dict[str, Any]],
    metrics: dict[str, Any],
    output_dir: Path,
    *,
    provider: str,
    fill_model: str = "pessimistic",
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    data_hash = hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()
    version_hash = hashlib.sha256(strategy_version.encode()).hexdigest()
    pack = {
        "name": name,
        "generated_at": datetime.now(UTC).isoformat(),
        "provider": provider,
        "data_range": [data[0].get("timestamp"), data[-1].get("timestamp")]
        if data
        else [None, None],
        "missing_data": sum(1 for row in data for value in row.values() if value is None),
        "transaction_cost_assumptions": {
            "fees_percent": 0.4,
            "slippage": "included by execution model",
        },
        "fill_model": fill_model,
        "metrics": metrics,
        "in_sample": metrics.get("in_sample", {}),
        "validation": metrics.get("validation", {}),
        "untouched_test": metrics.get("test", {}),
        "walk_forward": metrics.get("walk_forward", []),
        "parameter_sensitivity": metrics.get("parameter_sensitivity", []),
        "trade_distribution": metrics.get("trade_distribution", {}),
        "drawdown_periods": metrics.get("drawdown_periods", []),
        "benchmark_comparison": metrics.get("benchmark", {}),
        "regime_breakdown": metrics.get("regimes", {}),
        "liquidity_breakdown": metrics.get("liquidity", {}),
        "failure_cases": metrics.get("failure_cases", []),
        "reasons_not_to_trust": metrics.get(
            "reasons_not_to_trust",
            [
                "Limited history",
                "Provider and execution-model uncertainty",
                "No live execution evidence",
            ],
        ),
        "strategy_version_hash": version_hash,
        "data_snapshot_hash": data_hash,
        "no_profit_guarantee": "Historical paper results do not guarantee profit or future performance.",
    }
    json_path = output_dir / f"{name}_evidence.json"
    html_path = output_dir / f"{name}_evidence.html"
    ledger_path = output_dir / f"{name}_trades.csv"
    json_path.write_text(json.dumps(pack, indent=2, default=str), encoding="utf-8")
    html_path.write_text(
        f"<!doctype html><title>{name} evidence</title><h1>{name}</h1><p><strong>PAPER VALIDATION — {fill_model.upper()} FILL MODEL</strong></p><p>{pack['no_profit_guarantee']}</p><pre>{json.dumps(pack, indent=2, default=str)}</pre>",
        encoding="utf-8",
    )
    fields = sorted({key for trade in trades for key in trade}) or ["no_trades"]
    with ledger_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(trades)
    return {
        "json": str(json_path),
        "html": str(html_path),
        "trade_ledger": str(ledger_path),
        "strategy_version_hash": version_hash,
        "data_snapshot_hash": data_hash,
    }
