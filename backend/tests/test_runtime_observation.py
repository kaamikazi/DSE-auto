from __future__ import annotations

from app.services.runtime_observation import evaluate_runtime_observation


def samples(*, available_end: float = 2.4, restart_delta: int = 0) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for index in range(21):
        result.append(
            {
                "elapsed_seconds": index * 30,
                "available_gib": 2.5 + (available_end - 2.5) * index / 20,
                "commit_headroom_gib": 17.0,
                "pagefile_used_gib": 0.6,
                "pagefile_used_percent": 2.6,
                "hard_paging_per_second": 1.0,
                "container_restarts": {
                    "db": 7,
                    "worker": 7 + (restart_delta if index == 20 else 0),
                },
                "oom_killed": False,
                "process_missing": False,
            }
        )
    return result


def test_runtime_observation_accepts_stable_low_memory_runtime() -> None:
    result = evaluate_runtime_observation(
        samples(),
        project_footprint_gib=0.4,
        database_healthy=True,
        audit_valid=True,
    )
    assert result["passed"] is True
    assert result["metrics"]["required_physical_reserve_gib"] == 1.5


def test_runtime_observation_blocks_restart_delta_not_historical_count() -> None:
    result = evaluate_runtime_observation(
        samples(restart_delta=1),
        project_footprint_gib=0.4,
        database_healthy=True,
        audit_valid=True,
    )
    assert result["passed"] is False
    assert result["checks"]["container_restarts"] is False


def test_runtime_observation_blocks_short_or_declining_window() -> None:
    short_result = evaluate_runtime_observation(
        samples()[:5],
        project_footprint_gib=0.4,
        database_healthy=True,
        audit_valid=True,
    )
    declining_result = evaluate_runtime_observation(
        samples(available_end=1.9),
        project_footprint_gib=0.4,
        database_healthy=True,
        audit_valid=True,
    )
    assert short_result["passed"] is False
    assert short_result["checks"]["observation_window"] is False
    assert declining_result["passed"] is False
    assert declining_result["checks"]["memory_drop"] is False


def test_runtime_observation_blocks_database_or_audit_uncertainty() -> None:
    result = evaluate_runtime_observation(
        samples(),
        project_footprint_gib=0.4,
        database_healthy=False,
        audit_valid=False,
    )
    assert result["passed"] is False
    assert result["checks"]["database_health"] is False
    assert result["checks"]["audit_validity"] is False


def test_runtime_observation_allows_one_transient_paging_spike() -> None:
    observed = samples()
    observed[1]["hard_paging_per_second"] = 80.0
    result = evaluate_runtime_observation(
        observed,
        project_footprint_gib=0.4,
        database_healthy=True,
        audit_valid=True,
    )
    assert result["passed"] is True
    assert result["metrics"]["hard_paging_severe_samples"] == 1


def test_runtime_observation_blocks_sustained_paging_pressure() -> None:
    observed = samples()
    observed[8]["hard_paging_per_second"] = 80.0
    observed[9]["hard_paging_per_second"] = 80.0
    result = evaluate_runtime_observation(
        observed,
        project_footprint_gib=0.4,
        database_healthy=True,
        audit_valid=True,
    )
    assert result["passed"] is False
    assert result["checks"]["paging_pressure"] is False
