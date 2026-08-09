from __future__ import annotations

import sys

if __package__ in {None, ""}:
    sys.path.pop(0)

import csv
import json
import subprocess
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.core.config import assert_paper_only_safety, get_settings  # noqa: E402
from app.core.database import SessionLocal  # noqa: E402
from app.services.audit import verify_audit_chain  # noqa: E402
from app.services.cross_sectional_momentum import canonical_hash  # noqa: E402
from app.services.expanded_strategy_validation import (  # noqa: E402
    FROZEN_IDENTITIES,
    build_expanded_validation,
)
from app.services.historical_strategy_research import sha256_file  # noqa: E402


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Frozen-strategy 25-symbol validation",
        "",
        "Historical validation only. Qualification remains 0/60; no promotion or operational "
        "trading action occurred.",
        "",
        "| Strategy | Net return | Annualized | Max drawdown | Sharpe | Holdout | Assessment |",
        "|---|---:|---:|---:|---:|---:|---|",
    ]
    for identity in FROZEN_IDENTITIES:
        result = payload["strategies"][identity]
        window = result["common_25_symbol_overlap_window"]
        metrics = window["primary"]["metrics"]
        holdout = window["walk_forward"]["combined_holdout"]["metrics"]
        lines.append(
            f"| `{identity}` | {metrics['total_return_percent']:.3f}% | "
            f"{metrics['annualized_return_percent']:.3f}% | "
            f"{metrics['maximum_drawdown_percent']:.3f}% | "
            f"{metrics['sharpe_ratio'] if metrics['sharpe_ratio'] is not None else 'n/a'} | "
            f"{holdout['total_return_percent']:.3f}% | "
            f"`{result['expanded_validation_assessment']['assessment']}` |"
        )
    decision = payload["final_decision"]
    lines.extend(
        [
            "",
            f"Decision: `{decision['decision']}`.",
            (
                f"Strongest robustness candidate: `{decision['strongest_candidate']}`. "
                "It remains unpromoted and no paper session was created."
                if decision["strongest_candidate"]
                else "No strategy met every predeclared survival condition. Strategy discovery "
                "remains frozen; richer data/features or termination of price-only research is "
                "the next decision boundary."
            ),
            "",
            payload["important_limitation"],
            "",
            "Only adjusted, lineage-complete rows from the four immutable active datasets entered "
            "calculations. Unadjusted rows remained references; T3, conflicts, lifecycle holds, "
            "invalid rows, and duplicate conflicts remained excluded.",
            "",
            "Permanent safety stayed `TRADING_MODE=paper`, `LIVE_TRADING_ENABLED=false`, and "
            "`BROKER_ADAPTER=disabled`.",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> None:
    settings = get_settings()
    assert_paper_only_safety(settings)
    if _git("status", "--porcelain", "--untracked-files=no"):
        raise RuntimeError(
            "Tracked worktree must be clean before frozen-strategy validation"
        )
    head = _git("rev-parse", "HEAD")
    with SessionLocal() as db:
        if not verify_audit_chain(db):
            raise RuntimeError("Canonical audit chain is invalid")
        payload, rows = build_expanded_validation(db, ROOT, git_commit=head)

    payload_hash = canonical_hash(payload)
    output = (
        ROOT / "reports" / "strategy_validation" / f"expanded-25-{payload_hash[:12]}"
    )
    output.mkdir(parents=True, exist_ok=True)
    ledger_path = output / "strategy_window_comparison.csv"
    markdown_path = output / "interpretation.md"
    result_path = output / "validation_result.json"

    if not rows:
        raise RuntimeError("Comparison ledger is empty")
    with ledger_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    markdown_path.write_text(_markdown(payload), encoding="utf-8", newline="\n")
    payload["artifacts"] = {
        "canonical_payload_sha256": payload_hash,
        "strategy_window_comparison.csv": {
            "path": ledger_path.relative_to(ROOT).as_posix(),
            "sha256": sha256_file(ledger_path),
        },
        "interpretation.md": {
            "path": markdown_path.relative_to(ROOT).as_posix(),
            "sha256": sha256_file(markdown_path),
        },
    }
    result_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(
        json.dumps(
            {
                "decision": payload["final_decision"],
                "output": output.relative_to(ROOT).as_posix(),
                "canonical_payload_sha256": payload_hash,
                "result_file_sha256": sha256_file(result_path),
                "comparison_ledger_sha256": sha256_file(ledger_path),
                "interpretation_sha256": sha256_file(markdown_path),
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
