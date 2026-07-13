from __future__ import annotations

import argparse
import importlib.metadata
import json
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Export installed Python package licenses"
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    packages = []
    for distribution in importlib.metadata.distributions():
        metadata = distribution.metadata
        classifiers = metadata.get_all("Classifier") or []
        declared = [item for item in classifiers if item.startswith("License ::")]
        packages.append(
            {
                "name": metadata.get("Name", distribution.name),
                "version": distribution.version,
                "license": metadata.get("License") or None,
                "license_classifiers": declared,
                "homepage": metadata.get("Home-page")
                or metadata.get("Project-URL")
                or None,
            }
        )
    packages.sort(key=lambda item: str(item["name"]).lower())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(packages, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
