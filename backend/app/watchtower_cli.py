from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path

from app.core.database_identity import OPERATIONAL_SQLITE_PATH, REPOSITORY_ROOT
from app.services.watchtower import run_watchtower


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the local-only DSE Watchtower investigation scanner"
    )
    parser.add_argument(
        "--day-end-dir",
        type=Path,
        default=REPOSITORY_ROOT / "End of day",
        help="Directory containing immutable manually supplied DSE Day End files",
    )
    parser.add_argument(
        "--instrument-master",
        type=Path,
        default=REPOSITORY_ROOT / "config/watchtower_instrument_master.csv",
    )
    parser.add_argument(
        "--events",
        type=Path,
        default=REPOSITORY_ROOT / "config/watchtower_events.json",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=REPOSITORY_ROOT / "reports/watchtower",
    )
    parser.add_argument(
        "--protected-database",
        type=Path,
        default=OPERATIONAL_SQLITE_PATH,
        help="File hashed before and after the scan; it is never opened as a database",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = run_watchtower(
        day_end_directory=args.day_end_dir,
        instrument_master_path=args.instrument_master,
        event_evidence_path=args.events,
        output_root=args.output_dir,
        protected_database_path=args.protected_database,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
