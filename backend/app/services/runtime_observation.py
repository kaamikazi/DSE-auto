from __future__ import annotations

from statistics import fmean
from typing import Any

MINIMUM_OBSERVATION_SECONDS = 600
MINIMUM_COMMIT_HEADROOM_GIB = 8.0
MINIMUM_PHYSICAL_RESERVE_GIB = 1.5
MAXIMUM_AVAILABLE_DROP_GIB = 0.25
MAXIMUM_DECLINE_GIB_PER_MINUTE = 0.03
MAXIMUM_PAGEFILE_GROWTH_GIB = 0.25
MAXIMUM_PAGEFILE_USED_PERCENT = 50.0
MAXIMUM_AVERAGE_HARD_PAGING_PER_SECOND = 10.0
MAXIMUM_PEAK_HARD_PAGING_PER_SECOND = 50.0


def _number(sample: dict[str, Any], key: str) -> float:
    return float(sample.get(key) or 0.0)


def evaluate_runtime_observation(
    samples: list[dict[str, Any]],
    *,
    project_footprint_gib: float,
    database_healthy: bool,
    audit_valid: bool,
) -> dict[str, Any]:
    """Evaluate a measured low-memory runtime without relaxing the pre-start gate.

    Historical restart counters are accepted as a baseline. Only counter growth,
    OOM state, or process loss during the observation is blocking.
    """

    if not samples:
        return {
            "passed": False,
            "fail_closed": True,
            "checks": {"samples_present": False},
            "reason": "No runtime samples were supplied.",
        }

    ordered = sorted(samples, key=lambda item: _number(item, "elapsed_seconds"))
    first = ordered[0]
    last = ordered[-1]
    duration = _number(last, "elapsed_seconds") - _number(first, "elapsed_seconds")
    available = [_number(item, "available_gib") for item in ordered]
    commit_headroom = [_number(item, "commit_headroom_gib") for item in ordered]
    pagefile_used = [_number(item, "pagefile_used_gib") for item in ordered]
    pagefile_percent = [_number(item, "pagefile_used_percent") for item in ordered]
    hard_paging = [_number(item, "hard_paging_per_second") for item in ordered]
    consecutive_severe = 0
    maximum_consecutive_severe = 0
    for value in hard_paging:
        if value > MAXIMUM_PEAK_HARD_PAGING_PER_SECOND:
            consecutive_severe += 1
            maximum_consecutive_severe = max(maximum_consecutive_severe, consecutive_severe)
        else:
            consecutive_severe = 0

    elapsed_minutes = max(duration / 60.0, 1 / 60)
    available_drop = available[0] - available[-1]
    decline_per_minute = max(available_drop, 0.0) / elapsed_minutes
    adaptive_reserve = max(
        MINIMUM_PHYSICAL_RESERVE_GIB,
        round(max(project_footprint_gib, 0.0) * 1.25, 2),
    )

    initial_restarts = dict(first.get("container_restarts") or {})
    final_restarts = dict(last.get("container_restarts") or {})
    restart_deltas = {
        str(name): int(final_restarts.get(name, 0)) - int(initial_restarts.get(name, 0))
        for name in set(initial_restarts) | set(final_restarts)
    }
    restart_stable = all(delta == 0 for delta in restart_deltas.values())
    no_oom = not any(bool(item.get("oom_killed")) for item in ordered)
    no_process_loss = not any(bool(item.get("process_missing")) for item in ordered)

    checks = {
        "observation_window": duration >= MINIMUM_OBSERVATION_SECONDS,
        "commit_headroom": min(commit_headroom) >= MINIMUM_COMMIT_HEADROOM_GIB,
        "physical_reserve": min(available) >= adaptive_reserve,
        "memory_drop": available_drop <= MAXIMUM_AVAILABLE_DROP_GIB,
        "memory_trend": decline_per_minute <= MAXIMUM_DECLINE_GIB_PER_MINUTE,
        "pagefile_health": (
            max(pagefile_percent) <= MAXIMUM_PAGEFILE_USED_PERCENT
            and pagefile_used[-1] - pagefile_used[0] <= MAXIMUM_PAGEFILE_GROWTH_GIB
        ),
        "paging_pressure": (
            fmean(hard_paging) <= MAXIMUM_AVERAGE_HARD_PAGING_PER_SECOND
            and maximum_consecutive_severe <= 1
        ),
        "container_restarts": restart_stable,
        "no_oom_kill": no_oom,
        "process_stability": no_process_loss,
        "database_health": database_healthy,
        "audit_validity": audit_valid,
    }
    passed = all(checks.values())
    return {
        "passed": passed,
        "fail_closed": not passed,
        "checks": checks,
        "metrics": {
            "duration_seconds": round(duration, 1),
            "sample_count": len(ordered),
            "available_start_gib": available[0],
            "available_end_gib": available[-1],
            "available_minimum_gib": min(available),
            "available_drop_gib": round(available_drop, 3),
            "available_decline_gib_per_minute": round(decline_per_minute, 4),
            "commit_headroom_minimum_gib": min(commit_headroom),
            "project_footprint_gib": round(project_footprint_gib, 3),
            "required_physical_reserve_gib": adaptive_reserve,
            "pagefile_growth_gib": round(pagefile_used[-1] - pagefile_used[0], 3),
            "hard_paging_average_per_second": round(fmean(hard_paging), 2),
            "hard_paging_peak_per_second": max(hard_paging),
            "hard_paging_severe_samples": sum(
                value > MAXIMUM_PEAK_HARD_PAGING_PER_SECOND for value in hard_paging
            ),
            "hard_paging_maximum_consecutive_severe_samples": maximum_consecutive_severe,
            "restart_deltas": restart_deltas,
        },
        "basis": (
            "The 3 GiB pre-start floor remains unchanged. Post-start continuation requires "
            "a ten-minute stable observation, at least 8 GiB commit headroom, an adaptive "
            "physical reserve, healthy paging, no restart/OOM/process-loss evidence, and "
            "valid database and audit state."
        ),
    }
