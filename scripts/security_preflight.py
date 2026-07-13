from __future__ import annotations

import argparse
import json
import os
import re
import stat
import subprocess
from pathlib import Path
from typing import Any

SECRET_PATTERNS = (
    re.compile(
        r"(?i)(api[_-]?key|secret|token|password)\s*[:=]\s*['\"]?([A-Za-z0-9_./+\-=]{24,})"
    ),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
)
EXCLUDED_SUFFIXES = {".db", ".png", ".jpg", ".ico", ".woff", ".lock"}


def tracked_files(root: Path) -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    return [root / item.decode() for item in result.stdout.split(b"\0") if item]


def scan_secrets(root: Path) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for path in tracked_files(root):
        if path.suffix.lower() in EXCLUDED_SUFFIXES or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for number, line in enumerate(text.splitlines(), 1):
            if "change-me" in line or "example" in line.lower() or "${" in line:
                continue
            for pattern in SECRET_PATTERNS:
                match = pattern.search(line)
                if match is None:
                    continue
                captured = (
                    match.group(2) if match.lastindex and match.lastindex >= 2 else ""
                )
                if captured.lower().startswith(
                    ("settings.", "os.environ", "get_settings")
                ):
                    continue
                findings.append({"file": str(path.relative_to(root)), "line": number})
                break
    return findings


def configuration_permissions(root: Path) -> dict[str, Any]:
    env_path = root / ".env"
    if not env_path.exists():
        return {
            "path": ".env",
            "exists": False,
            "secure": True,
            "reason": "not present",
        }
    mode = stat.S_IMODE(env_path.stat().st_mode)
    if os.name == "nt":
        return {
            "path": ".env",
            "exists": True,
            "secure": None,
            "reason": "Review Windows ACL with Get-Acl; POSIX mode is not authoritative",
        }
    secure = not bool(mode & (stat.S_IRWXG | stat.S_IRWXO))
    return {"path": ".env", "exists": True, "secure": secure, "mode": oct(mode)}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Local secret and configuration preflight"
    )
    parser.add_argument(
        "--root", type=Path, default=Path(__file__).resolve().parents[1]
    )
    args = parser.parse_args()
    report = {
        "secret_findings": scan_secrets(args.root),
        "configuration_permissions": configuration_permissions(args.root),
        "public_binding_default": False,
    }
    print(json.dumps(report, indent=2))
    if report["secret_findings"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
