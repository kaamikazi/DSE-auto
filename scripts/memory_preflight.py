from __future__ import annotations

# ruff: noqa: E402 -- repository backend is intentionally placed first before imports.

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.services.memory_preflight import evaluate_workload_tiers, summarize_memory


def _markdown(report: dict[str, Any]) -> str:
    physical = report["physical_memory"]
    commit = report["commit_memory"]
    decision = report["preflight"]
    rows = [
        "# Windows Memory Diagnostics",
        "",
        f"Generated: `{report['generated_at']}`",
        f"Decision: **{decision['decision'].upper().replace('_', ' ')}**",
        "",
        "## Memory summary",
        "",
        f"- Total physical: {physical['total_gib']} GiB",
        f"- Available physical: {physical['available_gib']} GiB",
        f"- Committed: {commit['committed_gib']} GiB",
        f"- Commit limit: {commit['limit_gib']} GiB",
        f"- Commit headroom: {commit['headroom_gib']} GiB",
        f"- Pagefile allocated/used: {report['pagefile']['allocated_gib']} / {report['pagefile']['used_gib']} GiB",
        f"- Reclaimable estimate: {report['estimates']['reclaimable_gib_estimate']} GiB",
        f"- Non-reclaimable estimate: {report['estimates']['non_reclaimable_gib_estimate']} GiB",
        "",
        "## Workload tiers",
        "",
        "| Tier | Available requirement | Commit-headroom requirement | Result |",
        "| --- | ---: | ---: | --- |",
    ]
    for name, tier in decision["tiers"].items():
        rows.append(
            f"| {name} | {tier['minimum_available_gib']} GiB | "
            f"{tier['minimum_commit_headroom_gib']} GiB | "
            f"{'PASS' if tier['passed'] else 'BLOCKED'} |"
        )
    rows.extend(
        [
            "",
            "## Top processes by working set",
            "",
            "| Process | PID | Working set GiB | Private GiB |",
            "| --- | ---: | ---: | ---: |",
        ]
    )
    for item in report.get("top_by_working_set", []):
        rows.append(
            f"| {item['name']} | {item['pid']} | {item['working_set_gib']} | {item['private_gib']} |"
        )
    rows.extend(
        [
            "",
            "## Safe operator actions",
            "",
            "- Close or restart only operator-approved memory-heavy applications.",
            "- Prefer staged verification and stop `db_test` outside Stage A.",
            "- Reboot Windows if long-lived pools or compression remain unusually high.",
            "- Review Docker/WSL limits and cleanup commands manually; do not delete volumes or databases.",
            "- Do not clear standby memory, terminate processes, or change the pagefile automatically.",
        ]
    )
    return "\n".join(rows) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Evaluate a read-only Windows memory snapshot"
    )
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--json-output", type=Path, required=True)
    parser.add_argument("--markdown-output", type=Path, required=True)
    args = parser.parse_args()
    report: dict[str, Any] = json.loads(args.input.read_text(encoding="utf-8-sig"))
    report["estimates"] = summarize_memory(report)
    report["preflight"] = evaluate_workload_tiers(report)
    args.json_output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    args.markdown_output.write_text(_markdown(report), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
