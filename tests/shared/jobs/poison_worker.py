"""A generate worker with two threads (shared.jobs.worker.serve) where any job of a
task named `*-poison` kills the whole process, like an OOM or a segfault would.
Exits 0 once nothing is left to claim.

Usage: python poison_worker.py   (backend and lease timing from the env)
"""

import os
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from shared.storage import from_env  # noqa: E402
from shared.jobs.jobqueue import JobQueue  # noqa: E402
from shared.contracts.models import JobType  # noqa: E402
from shared.jobs.worker import serve  # noqa: E402


def fn(job):
    if job["task_id"].endswith("-poison"):
        time.sleep(0.2)  # let the sibling get claimed and start
        os._exit(137)
    time.sleep(0.5)
    return {"outcome": "OK"}


def main() -> None:
    backend = from_env()
    jq = JobQueue(backend)
    stop = threading.Event()

    def watch() -> None:
        while not stop.wait(0.1):
            counts = backend.store.open_job_counts()
            if counts["ready"] == 0 and counts["leased"] == 0 and counts["expired"] == 0:
                stop.set()

    threading.Thread(target=watch, daemon=True).start()
    serve(jq, JobType.GENERATE, fn, concurrency=2, stop=stop)


if __name__ == "__main__":
    main()
