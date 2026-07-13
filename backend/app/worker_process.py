from __future__ import annotations

import signal

from app.services.task_queue import TaskWorker, create_broker


def main() -> None:
    worker = TaskWorker(create_broker())

    def stop(_: int, __: object) -> None:
        worker.stop()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    worker.run_forever()


if __name__ == "__main__":
    main()
