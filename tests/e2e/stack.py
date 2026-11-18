"""
One copy of the whole pipeline in one process, with the fakes from fakes.py in
place of GLM-5.1, the Harbor gates and Harbor trials. Everything else is the
production code path:

    shared.jobs.worker.serve(generate)   ─┐
    shared.jobs.worker.serve(validate)    │  claim → heartbeat → run → settle
    evaluation.executor.Executor      ─┘  (+ the real classifier)
    job_service.JobService.run         decides every next step, publishes
    shared.jobs.reconciler                dead-letters, re-publishes to Redis

Backends come from the environment exactly as in production (DATABASE_URL,
REDIS_URL, S3_*, SDE_WORK_DIR). The process exits 0 once every task is final,
or keeps running if --forever. Kill it at any point; that's what the tests do.

Usage: python tests/e2e/stack.py [--forever]
"""

import logging
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import netguard  # noqa: E402

netguard.install()  # before anything can open a connection

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from evaluation.executor import Executor  # noqa: E402
from shared.jobs import reconciler  # noqa: E402
from shared.storage import from_env  # noqa: E402
from shared.jobs.jobqueue import JobQueue  # noqa: E402
from shared.contracts.models import DOCKER_SLOTS, JobType  # noqa: E402
from shared.jobs.worker import serve  # noqa: E402

from job_service.service import JobService  # noqa: E402

import fakes  # noqa: E402


def main() -> int:
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(process)d %(name)s %(message)s")
    backend = from_env()
    jq = JobQueue(backend)
    stop = threading.Event()
    executor = Executor(backend, run_trial=fakes.run_trial)
    service = JobService(backend)

    roles = [
        threading.Thread(target=serve, name="generate",
                         args=(jq, JobType.GENERATE, fakes.generate(backend)),
                         kwargs={"concurrency": 2, "stop": stop}),
        threading.Thread(target=serve, name="validate",
                         args=(jq, JobType.VALIDATE, fakes.validate(backend)),
                         kwargs={"concurrency": 2, "docker_slots": DOCKER_SLOTS, "stop": stop}),
        threading.Thread(target=executor.serve, name="executor", kwargs={"n_workers": DOCKER_SLOTS}),
        # Sweeps $SDE_ACCEPTED_DIR (this run's), like the Job Service publishes to.
        threading.Thread(target=reconciler.run, name="reconciler", args=(backend, stop, 0.5)),
    ]
    for t in roles:
        t.daemon = True
        t.start()

    service.run(until_done="--forever" not in sys.argv, stop=stop)
    stop.set()
    executor.shutdown()
    for t in roles:
        t.join(timeout=10)
    return 0


if __name__ == "__main__":
    sys.exit(main())
