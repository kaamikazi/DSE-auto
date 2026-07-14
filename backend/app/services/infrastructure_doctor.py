from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.services.memory_preflight import WORKLOAD_REQUIREMENTS

CommandRunner = Callable[[Sequence[str]], tuple[int, str]]
TcpProbe = Callable[[str, int], bool]
ResourceReader = Callable[[], dict[str, Any]]
WORKLOAD_SERVICES = {
    "database_only": ("db", "redis"),
    "integration_tests": ("db", "db_test", "redis"),
    "distributed_runtime": ("db", "redis"),
    "distributed_campaign": ("db", "redis"),
}


def _run(command: Sequence[str]) -> tuple[int, str]:
    try:
        result = subprocess.run(command, check=False, capture_output=True, text=True, timeout=20)
        output = "\n".join(item for item in (result.stdout.strip(), result.stderr.strip()) if item)
        return result.returncode, output.replace("\x00", "")
    except (OSError, subprocess.SubprocessError) as exc:
        return 127, f"{type(exc).__name__}: {exc}"


def _tcp(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except OSError:
        return False


def _resources() -> dict[str, Any]:
    disk = shutil.disk_usage(Path.cwd().anchor or Path.cwd())
    memory_total_gb: float | None = None
    memory_free_gb: float | None = None
    memory_committed_gb: float | None = None
    memory_commit_limit_gb: float | None = None
    virtualization: bool | None = None
    if os.name == "nt":
        code, output = _run(
            [
                "powershell.exe",
                "-NoProfile",
                "-Command",
                "$os=Get-CimInstance Win32_OperatingSystem;"
                "$mem=Get-CimInstance Win32_PerfFormattedData_PerfOS_Memory;"
                "$cpu=Get-CimInstance Win32_Processor|Select-Object -First 1;"
                "[pscustomobject]@{total=[math]::Round($os.TotalVisibleMemorySize/1MB,2);"
                "free=[math]::Round($os.FreePhysicalMemory/1MB,2);"
                "committed=[math]::Round($mem.CommittedBytes/1GB,2);"
                "commit_limit=[math]::Round($mem.CommitLimit/1GB,2);"
                "virtualization=$cpu.VirtualizationFirmwareEnabled}|ConvertTo-Json -Compress",
            ]
        )
        if code == 0:
            try:
                data = json.loads(output)
                memory_total_gb = float(data["total"])
                memory_free_gb = float(data["free"])
                memory_committed_gb = float(data["committed"])
                memory_commit_limit_gb = float(data["commit_limit"])
                virtualization = bool(data["virtualization"])
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                pass
    return {
        "disk_free_gb": round(disk.free / 1024**3, 2),
        "memory_total_gb": memory_total_gb,
        "memory_free_gb": memory_free_gb,
        "memory_committed_gb": memory_committed_gb,
        "memory_commit_limit_gb": memory_commit_limit_gb,
        "memory_commit_headroom_gb": (
            round(memory_commit_limit_gb - memory_committed_gb, 2)
            if memory_commit_limit_gb is not None and memory_committed_gb is not None
            else None
        ),
        "virtualization_available": virtualization,
    }


def _check(
    name: str,
    passed: bool,
    detail: Any,
    remediation: str,
    *,
    required: bool = True,
) -> dict[str, Any]:
    return {
        "name": name,
        "passed": passed,
        "required": required,
        "detail": detail,
        "remediation": "" if passed else remediation,
    }


def _compose_service_health(output: str) -> dict[str, dict[str, str]]:
    """Normalize Docker Compose JSON-lines/array output by service name."""
    records: list[Any] = []
    try:
        decoded = json.loads(output)
        records.extend(decoded if isinstance(decoded, list) else [decoded])
    except json.JSONDecodeError:
        for line in output.splitlines():
            if not line.strip():
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    services: dict[str, dict[str, str]] = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        service = str(record.get("Service", ""))
        if service:
            services[service] = {
                "state": str(record.get("State", "")).lower(),
                "health": str(record.get("Health", "")).lower(),
                "id": str(record.get("ID", "")),
            }
    return services


def run_infrastructure_doctor(
    output_dir: Path,
    *,
    workload_tier: str = "distributed_campaign",
    expect_application_ports: bool = False,
    runner: CommandRunner = _run,
    tcp_probe: TcpProbe = _tcp,
    resource_reader: ResourceReader = _resources,
) -> dict[str, Any]:
    if workload_tier not in WORKLOAD_REQUIREMENTS:
        raise ValueError(f"Unknown infrastructure workload tier: {workload_tier}")
    requirement = WORKLOAD_REQUIREMENTS[workload_tier]
    required_services = WORKLOAD_SERVICES[workload_tier]
    docker_path = shutil.which("docker")
    docker_code, docker_output = runner(["docker", "version"])
    compose_code, compose_output = runner(["docker", "compose", "version"])
    compose_ps_code, compose_ps_output = runner(
        ["docker", "compose", "ps", "--format", "json", *required_services]
    )
    wsl_code, wsl_output = runner(["wsl.exe", "--status"])
    service_code, service_output = runner(
        [
            "powershell.exe",
            "-NoProfile",
            "-Command",
            "(Get-Service -Name 'com.docker.service' -ErrorAction SilentlyContinue).Status",
        ]
    )
    resources = resource_reader()
    port_states = {str(port): tcp_probe("127.0.0.1", port) for port in (3000, 5432, 6379, 8000)}
    engine_available = docker_code == 0 and "server:" in docker_output.lower()
    service_running = service_code == 0 and "running" in service_output.lower()
    compose_services = _compose_service_health(compose_ps_output)
    required_containers_healthy = compose_ps_code == 0 and all(
        compose_services.get(service, {}).get("state") == "running"
        and compose_services.get(service, {}).get("health") == "healthy"
        for service in required_services
    )
    docker_runtime_demonstrably_ready = (
        engine_available and compose_code == 0 and required_containers_healthy
    )
    wsl2_available = wsl_code == 0 and (
        "version: 2" in wsl_output.lower()
        or "version 2" in wsl_output.lower()
        or "default version: 2" in wsl_output.lower()
    )
    application_ports_usable = not port_states["3000"] and (
        port_states["8000"] if expect_application_ports else not port_states["8000"]
    )
    data_ports_usable = all(not port_states[key] or key in {"5432", "6379"} for key in port_states)
    checks = [
        _check(
            "docker_desktop_installed",
            docker_path is not None,
            docker_path,
            "Install Docker Desktop for Windows from the approved installer, then rerun the doctor.",
        ),
        _check(
            "docker_service_running",
            service_running,
            {
                "windows_service": service_output or "service not found",
                "runtime_ready_without_service": docker_runtime_demonstrably_ready,
            },
            "Open Docker Desktop as the operator and wait for the engine to report Running; do not change Windows services from automation.",
            required=not docker_runtime_demonstrably_ready,
        ),
        _check(
            "linux_engine_available",
            engine_available,
            docker_output,
            "Start Docker Desktop and select the approved Linux-container/WSL2 engine, then verify `docker version` shows Client and Server.",
        ),
        _check(
            "docker_compose_available",
            compose_code == 0,
            compose_output,
            "Repair or upgrade the Docker Desktop Compose plugin until `docker compose version` succeeds.",
        ),
        _check(
            "required_containers_healthy",
            required_containers_healthy,
            {
                "compose_command_succeeded": compose_ps_code == 0,
                "required_services": list(required_services),
                "services": compose_services,
            },
            "Start the approved services listed for this workload tier and wait until every required Docker health check reports healthy.",
        ),
        _check(
            "wsl2_available",
            wsl2_available,
            wsl_output,
            "Have an administrator enable WSL2 and virtualization, reboot if required, and rerun `wsl --status`.",
        ),
        _check(
            "required_ports_usable",
            application_ports_usable and data_ports_usable,
            {
                "ports": port_states,
                "expect_application_ports": expect_application_ports,
                "frontend_expected": False,
                "api_expected": expect_application_ports,
            },
            "Confirm port 8000 matches the requested pre/post-start phase, keep port 3000 unused, and verify listeners on 5432/6379 are approved services.",
        ),
        _check(
            "disk_space",
            float(resources.get("disk_free_gb") or 0) >= 20,
            {"free_gb": resources.get("disk_free_gb"), "minimum_gb": 20},
            "Free at least 20 GB on the project/Docker data drive before pulling images or creating backups.",
        ),
        _check(
            "memory",
            (
                float(resources.get("memory_free_gb") or 0) >= requirement.minimum_available_gib
                and float(resources.get("memory_commit_headroom_gb") or 0)
                >= requirement.minimum_commit_headroom_gib
            ),
            {
                "workload_tier": workload_tier,
                "total_gb": resources.get("memory_total_gb"),
                "free_gb": resources.get("memory_free_gb"),
                "minimum_free_gb": requirement.minimum_available_gib,
                "commit_headroom_gb": resources.get("memory_commit_headroom_gb"),
                "minimum_commit_headroom_gb": requirement.minimum_commit_headroom_gib,
            },
            "Close memory-heavy applications or use only a lower workload tier whose physical and commit-headroom gates pass.",
        ),
        _check(
            "virtualization",
            resources.get("virtualization_available") is True,
            resources.get("virtualization_available"),
            "Have an administrator enable CPU virtualization in firmware/Windows features; automation must not change this setting.",
        ),
        _check(
            "postgresql_connectivity",
            port_states["5432"],
            {"host": "127.0.0.1", "port": 5432},
            "After Docker is healthy, start only the approved Compose PostgreSQL service and rerun the doctor.",
        ),
        _check(
            "redis_connectivity",
            port_states["6379"],
            {"host": "127.0.0.1", "port": 6379},
            "After Docker is healthy, start only the approved Compose Redis service and rerun the doctor.",
        ),
    ]
    ready = all(bool(item["passed"]) for item in checks if item["required"])
    report: dict[str, Any] = {
        "generated_at": datetime.now(UTC).isoformat(),
        "workload_tier": workload_tier,
        "ready": ready,
        "fail_closed": not ready,
        "checks": checks,
        "safety": {
            "configuration_changed": False,
            "docker_started": False,
            "trading_mode": "paper",
            "live_trading_enabled": False,
            "broker_adapter": "disabled",
        },
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "infrastructure_doctor.json"
    markdown_path = output_dir / "infrastructure_doctor.md"
    json_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    rows = [
        "# Infrastructure Doctor",
        "",
        f"Generated: `{report['generated_at']}`",
        f"Readiness: **{'READY' if ready else 'BLOCKED / FAIL CLOSED'}**",
        "",
        "| Check | Result | Detail | Remediation |",
        "| --- | --- | --- | --- |",
    ]
    for item in checks:
        detail = json.dumps(item["detail"], default=str).replace("|", "\\|").replace("\n", " ")
        remediation = str(item["remediation"]).replace("|", "\\|")
        result = "PASS" if item["passed"] else "FAIL" if item["required"] else "WARN"
        rows.append(f"| {item['name']} | {result} | {detail} | {remediation} |")
    markdown_path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    report["json_path"] = str(json_path)
    report["human_report_path"] = str(markdown_path)
    return report
