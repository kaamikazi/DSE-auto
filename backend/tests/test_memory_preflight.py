from __future__ import annotations

from pathlib import Path

from app.services.memory_preflight import evaluate_workload_tiers, summarize_memory

ROOT = Path(__file__).resolve().parents[2]


def snapshot(available: float, headroom: float) -> dict[str, object]:
    return {
        "physical_memory": {
            "total_gib": 16.0,
            "available_gib": available,
            "cache_gib": 0.5,
        },
        "commit_memory": {"headroom_gib": headroom},
    }


def test_memory_preflight_blocks_when_available_memory_is_insufficient() -> None:
    result = evaluate_workload_tiers(snapshot(1.4, 20.0))
    assert result["decision"] == "blocked"
    assert result["allowed_stages"] == []


def test_memory_preflight_blocks_when_commit_headroom_is_insufficient() -> None:
    result = evaluate_workload_tiers(snapshot(8.0, 3.9))
    assert result["decision"] == "blocked"
    assert result["tiers"]["distributed_campaign"]["available_ok"] is True
    assert result["tiers"]["distributed_campaign"]["commit_headroom_ok"] is False


def test_memory_preflight_selects_staged_mode() -> None:
    result = evaluate_workload_tiers(snapshot(2.5, 12.0))
    assert result["decision"] == "safe_only_in_stages"
    assert result["allowed_stages"] == ["A"]


def test_memory_preflight_allows_full_campaign_only_with_both_margins() -> None:
    result = evaluate_workload_tiers(snapshot(4.5, 12.0))
    assert result["decision"] == "safe_to_continue"
    assert result["allowed_stages"] == ["A", "B", "C"]


def test_memory_summary_does_not_double_count_cache() -> None:
    result = summarize_memory(snapshot(2.0, 10.0))
    assert result["reclaimable_gib_estimate"] == 2.0
    assert result["non_reclaimable_gib_estimate"] == 14.0


def test_stage_start_checks_memory_before_changing_services() -> None:
    script = (ROOT / "scripts/start_infrastructure_stage.ps1").read_text(encoding="utf-8")
    assert script.index("memory_doctor.ps1") < script.index("docker compose up")
    assert script.index("docker compose stop db_test") < script.index(
        "docker compose --profile production-like up"
    )
    assert "frontend" not in script


def test_stage_stop_preserves_primary_data_services_and_volumes() -> None:
    script = (ROOT / "scripts/stop_infrastructure_stage.ps1").read_text(encoding="utf-8")
    assert "docker compose down" not in script
    assert "docker volume" not in script
    assert "stop -t 30 db_test" in script
    assert "worker_2 worker scheduler backend" in script
