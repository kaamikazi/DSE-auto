from __future__ import annotations

import signal

from app.services.external_scheduler import ExternalScheduler


def main() -> None:
    scheduler = ExternalScheduler()

    def stop(_: int, __: object) -> None:
        scheduler.stop()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    scheduler.run_forever()


if __name__ == "__main__":
    main()
