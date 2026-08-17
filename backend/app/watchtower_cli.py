from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path

from app.core.database_identity import OPERATIONAL_SQLITE_PATH, REPOSITORY_ROOT
from app.services.watchtower import run_watchtower
from app.services.watchtower_instrument_master import build_local_instrument_master


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
        "--instrument-provenance",
        type=Path,
        default=REPOSITORY_ROOT / "config/watchtower_instrument_master.provenance.json",
    )
    parser.add_argument(
        "--instrument-evidence-dir",
        type=Path,
        default=REPOSITORY_ROOT / "Market evidence/instrument master",
        help="Directory containing immutable, manually saved official instrument pages",
    )
    parser.add_argument(
        "--refresh-instrument-master",
        action="store_true",
        help="Rebuild the local master and provenance from saved local HTML before scanning",
    )
    parser.add_argument(
        "--events",
        type=Path,
        default=REPOSITORY_ROOT / "config/watchtower_events.json",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=REPOSITORY_ROOT / "reports/watchtower/v0.2.0",
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
    master_build = None
    if args.refresh_instrument_master:
        master_build = build_local_instrument_master(
            evidence_directory=args.instrument_evidence_dir,
            instrument_master_path=args.instrument_master,
            provenance_path=args.instrument_provenance,
            repository_root=REPOSITORY_ROOT,
        )
    result = run_watchtower(
        day_end_directory=args.day_end_dir,
        instrument_master_path=args.instrument_master,
        event_evidence_path=args.events,
        output_root=args.output_dir,
        protected_database_path=args.protected_database,
        instrument_provenance_path=args.instrument_provenance,
    )
    if master_build is not None:
        result["instrument_master_build"] = master_build.payload()
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
