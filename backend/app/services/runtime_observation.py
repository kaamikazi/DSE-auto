from __future__ import annotations

from datetime import UTC, datetime
from statistics import fmean
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import WorkerHeartbeat

MINIMUM_OBSERVATION_SECONDS = 600
MINIMUM_COMMIT_HEADROOM_GIB = 8.0
MINIMUM_PHYSICAL_RESERVE_GIB = 1.5
MAXIMUM_AVAILABLE_DROP_GIB = 0.25
MAXIMUM_DECLINE_GIB_PER_MINUTE = 0.03
MAXIMUM_PAGEFILE_GROWTH_GIB = 0.25
MAXIMUM_PAGEFILE_USED_PERCENT = 50.0
PAGES_INPUT_WARNING_PER_SECOND = 500.0
PAGE_READS_WARNING_PER_SECOND = 5.0
DISK_LATENCY_WARNING_MS = 50.0
DISK_QUEUE_WARNING = 2.0
SCHEDULER_LAG_WARNING_SECONDS = 10.0
WORKER_HEARTBEAT_WARNING_SECONDS = 60.0
MINIMUM_CONSECUTIVE_PRESSURE_SAMPLES = 2


def _number(sample: dict[str, Any], key: str) -> float:
    return float(sample.get(key) or 0.0)


def _optional_number(sample: dict[str, Any], key: str) -> float | None:
    value = sample.get(key)
    return None if value is None else float(value)


def _maximum_consecutive(values: list[bool]) -> int:
    current = 0
    maximum = 0
    for value in values:
        current = current + 1 if value else 0
        maximum = max(maximum, current)
    return maximum


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def runtime_operational_delays(db: Session, now: datetime | None = None) -> dict[str, float | None]:
    """Read scheduler and worker heartbeat ages without changing runtime state."""

    current = now or datetime.now(UTC)
    records = db.scalars(
        select(WorkerHeartbeat).where(WorkerHeartbeat.state.in_(["starting", "running"]))
    ).all()
    scheduler_ages: list[float] = []
    worker_ages: list[float] = []
    for record in records:
        age = max(0.0, (current - _aware(record.heartbeat_at)).total_seconds())
        is_scheduler = record.worker_id.startswith("scheduler:") or "scheduler" in record.queues
        (scheduler_ages if is_scheduler else worker_ages).append(age)
    return {
        "scheduler_lag_seconds": max(scheduler_ages) if scheduler_ages else None,
        "worker_heartbeat_delay_seconds": max(worker_ages) if worker_ages else None,
    }


def evaluate_runtime_observation(
    samples: list[dict[str, Any]],
    *,
    project_footprint_gib: float,
    database_healthy: bool,
    audit_valid: bool,
) -> dict[str, Any]:
    """Evaluate steady-state runtime using paging and observable consequences.

    Startup warm-up and shutdown samples remain evidence but do not enter the
    steady-state decision. Hard faults can read executable, DLL, or mapped-file
    pages and therefore become blocking only when sustained input/read activity
    coincides with memory, pagefile, disk, or operational degradation.
    """

    if not samples:
        return {
            "passed": False,
            "fail_closed": True,
            "checks": {"samples_present": False},
            "reason": "No runtime samples were supplied.",
        }

    ordered = sorted(samples, key=lambda item: _number(item, "elapsed_seconds"))
    steady = [item for item in ordered if item.get("phase", "steady_state") == "steady_state"]
    if not steady:
        return {
            "passed": False,
            "fail_closed": True,
            "checks": {"steady_state_samples_present": False},
            "reason": "No steady-state runtime samples were supplied.",
        }

    required_signal_keys = {
        "page_faults_per_second",
        "pages_input_per_second",
        "page_reads_per_second",
        "disk_read_latency_ms",
        "disk_queue_length",
        "scheduler_lag_seconds",
        "worker_heartbeat_delay_seconds",
    }
    diagnostic_complete = all(
        all(key in sample and sample[key] is not None for key in required_signal_keys)
        for sample in steady
    )

    first = steady[0]
    last = steady[-1]
    duration = _number(last, "elapsed_seconds") - _number(first, "elapsed_seconds")
    available = [_number(item, "available_gib") for item in steady]
    commit_headroom = [_number(item, "commit_headroom_gib") for item in steady]
    pagefile_used = [_number(item, "pagefile_used_gib") for item in steady]
    pagefile_percent = [_number(item, "pagefile_used_percent") for item in steady]
    page_faults = [_number(item, "page_faults_per_second") for item in steady]
    pages_input = [_number(item, "pages_input_per_second") for item in steady]
    page_reads = [_number(item, "page_reads_per_second") for item in steady]
    disk_read_latency = [_number(item, "disk_read_latency_ms") for item in steady]
    disk_queue = [_number(item, "disk_queue_length") for item in steady]
    scheduler_lag = [_optional_number(item, "scheduler_lag_seconds") for item in steady]
    worker_delay = [_optional_number(item, "worker_heartbeat_delay_seconds") for item in steady]

    elapsed_minutes = max(duration / 60.0, 1 / 60)
    available_drop = available[0] - available[-1]
    decline_per_minute = max(available_drop, 0.0) / elapsed_minutes
    adaptive_reserve = max(
        MINIMUM_PHYSICAL_RESERVE_GIB,
        round(max(project_footprint_gib, 0.0) * 1.25, 2),
    )
    pagefile_growth = pagefile_used[-1] - pagefile_used[0]

    initial_restarts = dict(first.get("container_restarts") or {})
    final_restarts = dict(last.get("container_restarts") or {})
    restart_deltas = {
        str(name): int(final_restarts.get(name, 0)) - int(initial_restarts.get(name, 0))
        for name in set(initial_restarts) | set(final_restarts)
    }
    restart_stable = all(delta == 0 for delta in restart_deltas.values())
    no_oom = not any(bool(item.get("oom_killed")) for item in steady)
    no_process_loss = not any(bool(item.get("process_missing")) for item in steady)

    paging_samples = [
        page_input > PAGES_INPUT_WARNING_PER_SECOND or page_read > PAGE_READS_WARNING_PER_SECOND
        for page_input, page_read in zip(pages_input, page_reads, strict=True)
    ]
    sustained_paging = _maximum_consecutive(paging_samples) >= MINIMUM_CONSECUTIVE_PRESSURE_SAMPLES
    disk_pressure_samples = [
        latency > DISK_LATENCY_WARNING_MS or queue > DISK_QUEUE_WARNING
        for latency, queue in zip(disk_read_latency, disk_queue, strict=True)
    ]
    sustained_disk_pressure = (
        _maximum_consecutive(disk_pressure_samples) >= MINIMUM_CONSECUTIVE_PRESSURE_SAMPLES
    )
    scheduler_degraded = any(
        value is not None and value > SCHEDULER_LAG_WARNING_SECONDS for value in scheduler_lag
    )
    heartbeat_degraded = any(
        value is not None and value > WORKER_HEARTBEAT_WARNING_SECONDS for value in worker_delay
    )
    memory_consequence = (
        min(available) < adaptive_reserve
        or available_drop > MAXIMUM_AVAILABLE_DROP_GIB
        or decline_per_minute > MAXIMUM_DECLINE_GIB_PER_MINUTE
    )
    pagefile_consequence = (
        max(pagefile_percent) > MAXIMUM_PAGEFILE_USED_PERCENT
        or pagefile_growth > MAXIMUM_PAGEFILE_GROWTH_GIB
    )
    instability_consequence = not restart_stable or not no_oom or not no_process_loss
    paging_consequences = {
        "memory_pressure": memory_consequence,
        "pagefile_growth_or_usage": pagefile_consequence,
        "sustained_disk_pressure": sustained_disk_pressure,
        "scheduler_lag": scheduler_degraded,
        "worker_heartbeat_delay": heartbeat_degraded,
        "process_instability": instability_consequence,
        "database_instability": not database_healthy,
    }
    paging_failure = sustained_paging and any(paging_consequences.values())
    paging_warning = any(paging_samples) and not paging_failure
    paging_classification = "fail" if paging_failure else "warning" if paging_warning else "healthy"

    checks = {
        "observation_window": duration >= MINIMUM_OBSERVATION_SECONDS,
        "diagnostic_complete": diagnostic_complete,
        "commit_headroom": min(commit_headroom) >= MINIMUM_COMMIT_HEADROOM_GIB,
        "physical_reserve": min(available) >= adaptive_reserve,
        "memory_drop": available_drop <= MAXIMUM_AVAILABLE_DROP_GIB,
        "memory_trend": decline_per_minute <= MAXIMUM_DECLINE_GIB_PER_MINUTE,
        "pagefile_health": not pagefile_consequence,
        "paging_impact": not paging_failure,
        "container_restarts": restart_stable,
        "no_oom_kill": no_oom,
        "process_stability": no_process_loss,
        "scheduler_responsiveness": not scheduler_degraded,
        "worker_heartbeat_health": not heartbeat_degraded,
        "database_health": database_healthy,
        "audit_validity": audit_valid,
    }
    passed = all(checks.values())
    return {
        "passed": passed,
        "fail_closed": not passed,
        "paging_classification": paging_classification,
        "warnings": (
            ["Hard-fault disk reads had no measured operational consequence."]
            if paging_warning
            else []
        ),
        "checks": checks,
        "metrics": {
            "steady_state_duration_seconds": round(duration, 1),
            "startup_warmup_sample_count": sum(
                item.get("phase") == "startup_warmup" for item in ordered
            ),
            "steady_state_sample_count": len(steady),
            "shutdown_sample_count": sum(
                item.get("phase") == "shutdown_activity" for item in ordered
            ),
            "available_start_gib": available[0],
            "available_end_gib": available[-1],
            "available_minimum_gib": min(available),
            "available_drop_gib": round(available_drop, 3),
            "available_decline_gib_per_minute": round(decline_per_minute, 4),
            "commit_headroom_minimum_gib": min(commit_headroom),
            "project_footprint_gib": round(project_footprint_gib, 3),
            "required_physical_reserve_gib": adaptive_reserve,
            "pagefile_growth_gib": round(pagefile_growth, 3),
            "page_faults_average_per_second": round(fmean(page_faults), 2),
            "page_faults_peak_per_second": max(page_faults),
            "pages_input_average_per_second": round(fmean(pages_input), 2),
            "pages_input_peak_per_second": max(pages_input),
            "page_reads_average_per_second": round(fmean(page_reads), 2),
            "page_reads_peak_per_second": max(page_reads),
            "sustained_paging": sustained_paging,
            "disk_read_latency_peak_ms": max(disk_read_latency),
            "disk_queue_peak": max(disk_queue),
            "scheduler_lag_peak_seconds": max(
                (value for value in scheduler_lag if value is not None), default=None
            ),
            "worker_heartbeat_delay_peak_seconds": max(
                (value for value in worker_delay if value is not None), default=None
            ),
            "paging_consequences": paging_consequences,
            "restart_deltas": restart_deltas,
        },
        "basis": (
            "The 3 GiB pre-start floor remains unchanged. The steady-state decision excludes "
            "startup warm-up and shutdown activity. Hard-fault disk reads alone are warning "
            "evidence; paging fails only when sustained input/read activity coincides with a "
            "memory, pagefile, disk, scheduler, heartbeat, process, or database consequence."
        ),
    }
