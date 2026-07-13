from __future__ import annotations

import argparse
import json

from app.core.database import SessionLocal
from app.services.distributed_simulation import run_distributed_simulation_phase
from app.services.task_queue import create_broker


def main() -> None:
    parser = argparse.ArgumentParser(description="Accelerated distributed paper simulation")
    parser.add_argument("--campaign", default="m7-distributed-30-day")
    parser.add_argument("--start-day", type=int, required=True)
    parser.add_argument("--end-day", type=int, required=True)
    parser.add_argument("--require-distributed", action="store_true")
    args = parser.parse_args()
    with SessionLocal() as db:
        result = run_distributed_simulation_phase(
            db,
            create_broker(),
            campaign_name=args.campaign,
            start_day=args.start_day,
            end_day=args.end_day,
            require_distributed=args.require_distributed,
        )
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
