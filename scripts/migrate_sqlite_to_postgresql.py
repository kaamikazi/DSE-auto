from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.services.database_migration import migrate_sqlite_to_postgresql


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Non-destructive SQLite to PostgreSQL migration"
    )
    parser.add_argument("--source", required=True)
    parser.add_argument("--destination", required=True)
    parser.add_argument(
        "--execute", action="store_true", help="Copy into an empty migrated database"
    )
    args = parser.parse_args()
    result = migrate_sqlite_to_postgresql(
        args.source, args.destination, dry_run=not args.execute
    )
    print(json.dumps(result, indent=2, default=str))
    if args.execute and not result["verified"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
