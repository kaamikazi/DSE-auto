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

CommandRunner = Callable[[Sequence[str]], tuple[int, str]]
TcpProbe = Callable[[str, int], bool]
ResourceReader = Callable[[], dict[str, Any]]


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
    virtualization: bool | None = None
    if os.name == "nt":
        code, output = _run(
            [
                "powershell.exe",
                "-NoProfile",
                "-Command",
                "$os=Get-CimInstance Win32_OperatingSystem;"
                "$cpu=Get-CimInstance Win32_Processor|Select-Object -First 1;"
                "[pscustomobject]@{total=[math]::Round($os.TotalVisibleMemorySize/1MB,2);"
                "free=[math]::Round($os.FreePhysicalMemory/1MB,2);"
                "virtualization=$cpu.VirtualizationFirmwareEnabled}|ConvertTo-Json -Compress",
            ]
        )
        if code == 0:
            try:
                data = json.loads(output)
                memory_total_gb = float(data["total"])
                memory_free_gb = float(data["free"])
                virtualization = bool(data["virtualization"])
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                pass
    return {
        "disk_free_gb": round(disk.free / 1024**3, 2),
        "memory_total_gb": memory_total_gb,
        "memory_free_gb": memory_free_gb,
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


def run_infrastructure_doctor(
    output_dir: Path,
    *,
    runner: CommandRunner = _run,
    tcp_probe: TcpProbe = _tcp,
    resource_reader: ResourceReader = _resources,
) -> dict[str, Any]:
    docker_path = shutil.which("docker")
    docker_code, docker_output = runner(["docker", "version"])
    compose_code, compose_output = runner(["docker", "compose", "version"])
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
    wsl2_available = wsl_code == 0 and (
        "version: 2" in wsl_output.lower()
        or "version 2" in wsl_output.lower()
        or "default version: 2" in wsl_output.lower()
    )
    free_ports = all(not value for key, value in port_states.items() if key in {"3000", "8000"})
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
            service_output or "service not found",
            "Open Docker Desktop as the operator and wait for the engine to report Running; do not change Windows services from automation.",
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
            "wsl2_available",
            wsl2_available,
            wsl_output,
            "Have an administrator enable WSL2 and virtualization, reboot if required, and rerun `wsl --status`.",
        ),
        _check(
            "required_ports_usable",
            free_ports and data_ports_usable,
            port_states,
            "Stop or reconfigure the conflicting process on 3000/8000. Confirm any listener on 5432/6379 is the intended isolated service.",
        ),
        _check(
            "disk_space",
            float(resources.get("disk_free_gb") or 0) >= 20,
            {"free_gb": resources.get("disk_free_gb"), "minimum_gb": 20},
            "Free at least 20 GB on the project/Docker data drive before pulling images or creating backups.",
        ),
        _check(
            "memory",
            float(resources.get("memory_free_gb") or 0) >= 4,
            {
                "total_gb": resources.get("memory_total_gb"),
                "free_gb": resources.get("memory_free_gb"),
                "minimum_free_gb": 4,
            },
            "Close memory-heavy applications or increase available RAM until at least 4 GB is free before the distributed exercise.",
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
        rows.append(
            f"| {item['name']} | {'PASS' if item['passed'] else 'FAIL'} | {detail} | {remediation} |"
        )
    markdown_path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    report["json_path"] = str(json_path)
    report["human_report_path"] = str(markdown_path)
    return report
