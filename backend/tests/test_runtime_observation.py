from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from app.models import WorkerHeartbeat
from app.services.runtime_observation import (
    evaluate_runtime_observation,
    runtime_operational_delays,
)


def samples(*, available_end: float = 2.4, restart_delta: int = 0) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for index in range(5):
        result.append(
            {
                "phase": "startup_warmup",
                "elapsed_seconds": index * 30,
                "page_faults_per_second": 10.0,
                "pages_input_per_second": 0.0,
                "page_reads_per_second": 0.0,
            }
        )
    for index in range(21):
        result.append(
            {
                "phase": "steady_state",
                "elapsed_seconds": 150 + index * 30,
                "available_gib": 2.5 + (available_end - 2.5) * index / 20,
                "commit_headroom_gib": 17.0,
                "pagefile_used_gib": 0.6,
                "pagefile_used_percent": 2.6,
                "page_faults_per_second": 10.0,
                "pages_input_per_second": 0.0,
                "page_reads_per_second": 0.0,
                "disk_read_latency_ms": 2.0,
                "disk_queue_length": 0.1,
                "scheduler_lag_seconds": 1.0,
                "worker_heartbeat_delay_seconds": 5.0,
                "container_restarts": {
                    "db": 7,
                    "worker": 7 + (restart_delta if index == 20 else 0),
                },
                "oom_killed": False,
                "process_missing": False,
            }
        )
    result.append(
        {
            "phase": "shutdown_activity",
            "elapsed_seconds": 780,
            "page_faults_per_second": 50_000.0,
            "pages_input_per_second": 5_000.0,
            "page_reads_per_second": 500.0,
        }
    )
    return result


def evaluate(observed: list[dict[str, object]]) -> dict[str, object]:
    return evaluate_runtime_observation(
        observed,
        project_footprint_gib=0.4,
        database_healthy=True,
        audit_valid=True,
    )


def steady(observed: list[dict[str, object]]) -> list[dict[str, object]]:
    return [item for item in observed if item.get("phase") == "steady_state"]


def test_runtime_observation_accepts_stable_steady_state() -> None:
    result = evaluate(samples())
    assert result["passed"] is True
    assert result["paging_classification"] == "healthy"
    assert result["metrics"]["startup_warmup_sample_count"] == 5  # type: ignore[index]
    assert result["metrics"]["shutdown_sample_count"] == 1  # type: ignore[index]


def test_startup_page_loading_spike_is_excluded() -> None:
    observed = samples()
    observed[0]["page_faults_per_second"] = 100_000.0
    observed[0]["pages_input_per_second"] = 10_000.0
    observed[0]["page_reads_per_second"] = 1_000.0
    result = evaluate(observed)
    assert result["passed"] is True
    assert result["paging_classification"] == "healthy"


def test_file_backed_reads_without_pagefile_pressure_are_warning() -> None:
    observed = samples()
    for item in steady(observed)[5:8]:
        item["pages_input_per_second"] = 900.0
        item["page_reads_per_second"] = 20.0
    result = evaluate(observed)
    assert result["passed"] is True
    assert result["paging_classification"] == "warning"
    assert result["checks"]["paging_impact"] is True  # type: ignore[index]


def test_single_steady_state_paging_spike_is_warning() -> None:
    observed = samples()
    item = steady(observed)[5]
    item["pages_input_per_second"] = 5_000.0
    item["page_reads_per_second"] = 500.0
    result = evaluate(observed)
    assert result["passed"] is True
    assert result["paging_classification"] == "warning"


def test_high_faults_with_stable_ram_and_latency_do_not_fail() -> None:
    observed = samples()
    for item in steady(observed):
        item["page_faults_per_second"] = 75_000.0
    result = evaluate(observed)
    assert result["passed"] is True
    assert result["paging_classification"] == "healthy"


def test_genuine_sustained_pagefile_thrashing_fails() -> None:
    observed = samples()
    observed_steady = steady(observed)
    for item in observed_steady[8:11]:
        item["pages_input_per_second"] = 1_500.0
        item["page_reads_per_second"] = 40.0
        item["disk_read_latency_ms"] = 90.0
        item["disk_queue_length"] = 5.0
    observed_steady[-1]["pagefile_used_gib"] = 1.0
    result = evaluate(observed)
    assert result["passed"] is False
    assert result["paging_classification"] == "fail"
    assert result["checks"]["paging_impact"] is False  # type: ignore[index]
    consequences = result["metrics"]["paging_consequences"]  # type: ignore[index]
    assert consequences["pagefile_growth_or_usage"] is True
    assert consequences["sustained_disk_pressure"] is True


def test_falling_memory_fails_without_relying_on_paging() -> None:
    result = evaluate(samples(available_end=1.9))
    assert result["passed"] is False
    assert result["checks"]["memory_drop"] is False  # type: ignore[index]
    assert result["paging_classification"] == "healthy"


def test_sustained_paging_with_scheduler_lag_fails() -> None:
    observed = samples()
    for item in steady(observed)[8:11]:
        item["pages_input_per_second"] = 900.0
        item["page_reads_per_second"] = 20.0
        item["scheduler_lag_seconds"] = 30.0
    result = evaluate(observed)
    assert result["passed"] is False
    assert result["paging_classification"] == "fail"
    assert result["checks"]["scheduler_responsiveness"] is False  # type: ignore[index]


def test_oom_and_process_instability_fail() -> None:
    observed = samples(restart_delta=1)
    steady(observed)[10]["oom_killed"] = True
    result = evaluate(observed)
    assert result["passed"] is False
    assert result["checks"]["container_restarts"] is False  # type: ignore[index]
    assert result["checks"]["no_oom_kill"] is False  # type: ignore[index]


def test_missing_new_paging_signals_fails_closed() -> None:
    observed = samples()
    del steady(observed)[0]["pages_input_per_second"]
    result = evaluate(observed)
    assert result["passed"] is False
    assert result["checks"]["diagnostic_complete"] is False  # type: ignore[index]


def test_runtime_operational_delays_separate_scheduler_and_workers(db: Session) -> None:
    now = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)
    db.add_all(
        [
            WorkerHeartbeat(
                worker_id="scheduler:test:1",
                process_id=1,
                state="running",
                queues=["scheduler"],
                heartbeat_at=now - timedelta(seconds=3),
            ),
            WorkerHeartbeat(
                worker_id="worker:test:2",
                process_id=2,
                state="running",
                queues=["dse-paper-tasks"],
                heartbeat_at=now - timedelta(seconds=7),
            ),
        ]
    )
    db.commit()
    assert runtime_operational_delays(db, now) == {
        "scheduler_lag_seconds": 3.0,
        "worker_heartbeat_delay_seconds": 7.0,
    }
