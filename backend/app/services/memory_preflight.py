from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class MemoryRequirement:
    minimum_available_gib: float
    minimum_commit_headroom_gib: float
    purpose: str


WORKLOAD_REQUIREMENTS: dict[str, MemoryRequirement] = {
    "database_only": MemoryRequirement(
        1.5,
        4.0,
        "PostgreSQL, test PostgreSQL, and Redis health/administration",
    ),
    "integration_tests": MemoryRequirement(
        2.0,
        6.0,
        "Database and Redis integration tests without application processes",
    ),
    "distributed_runtime": MemoryRequirement(
        3.0,
        8.0,
        "API, scheduler, two workers, PostgreSQL, and Redis",
    ),
    "distributed_campaign": MemoryRequirement(
        4.0,
        10.0,
        "Accelerated campaign with distributed runtime and evidence generation",
    ),
}


def evaluate_workload_tiers(report: dict[str, Any]) -> dict[str, Any]:
    physical = report.get("physical_memory", {})
    commit = report.get("commit_memory", {})
    available = float(physical.get("available_gib") or 0)
    commit_headroom = float(commit.get("headroom_gib") or 0)
    tiers: dict[str, dict[str, Any]] = {}
    for name, requirement in WORKLOAD_REQUIREMENTS.items():
        available_ok = available >= requirement.minimum_available_gib
        commit_ok = commit_headroom >= requirement.minimum_commit_headroom_gib
        tiers[name] = {
            **asdict(requirement),
            "available_gib": available,
            "commit_headroom_gib": commit_headroom,
            "available_ok": available_ok,
            "commit_headroom_ok": commit_ok,
            "passed": available_ok and commit_ok,
        }
    allowed_stages: list[str] = []
    if tiers["integration_tests"]["passed"]:
        allowed_stages.append("A")
    if tiers["distributed_runtime"]["passed"]:
        allowed_stages.append("B")
    if tiers["distributed_campaign"]["passed"]:
        allowed_stages.append("C")
    if "C" in allowed_stages:
        decision = "safe_to_continue"
    elif allowed_stages:
        decision = "safe_only_in_stages"
    else:
        decision = "blocked"
    return {
        "decision": decision,
        "allowed_stages": allowed_stages,
        "tiers": tiers,
        "basis": (
            "Conservative engineering budgets pending measured peak evidence; "
            "both physical availability and commit headroom are mandatory."
        ),
    }


def summarize_memory(report: dict[str, Any]) -> dict[str, Any]:
    physical = report.get("physical_memory", {})
    available = float(physical.get("available_gib") or 0)
    total = float(physical.get("total_gib") or 0)
    cache = float(physical.get("cache_gib") or 0)
    return {
        "reclaimable_gib_estimate": round(max(available, cache), 2),
        "non_reclaimable_gib_estimate": round(max(total - available, 0), 2),
        "note": (
            "Available memory already includes reclaimable standby/cache pages; estimates "
            "must not be added together."
        ),
    }
