"""
One copy of the whole pipeline in one process, with the fakes from fakes.py in
place of GLM-5.1 (drafting and the failure review), the Harbor gates and Harbor
trials. Everything else is the production code path:

    shared.jobs.worker.serve_generation    ─┐  claim → heartbeat → run → settle,
      (create, revise; world in agent)   │  each settle handing off to the next
    shared.jobs.worker.serve_validate       │  stage in the same transaction
      (validate; world_check in agent)   │
    evaluation.executor.Executor         │
    evaluation.trace_parser_service      ─┘  (+ the real classifier and trace parser)
    shared.jobs.reconciler                  dead-letters, re-publishes to Redis

Backends come from the environment exactly as in production (DATABASE_URL,
REDIS_URL, S3_*, SDE_WORK_DIR). The process exits 0 once every task is final and
every accepted one is published, or keeps running if --forever. Kill it at any
point; that's what the tests do.

Usage: python tests/e2e/stack.py [--forever]
"""

import logging
import os
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import netguard  # noqa: E402

netguard.install()  # before anything can open a connection

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from evaluation.executor import Executor  # noqa: E402
from evaluation.trace_parser_service import TraceParserService  # noqa: E402
from shared.jobs import reconciler  # noqa: E402
from shared.storage import from_env  # noqa: E402
from shared.contracts.models import DOCKER_SLOTS, POLL_S  # noqa: E402
from shared.jobs.worker import serve_generation, serve_validate  # noqa: E402

import fakes  # noqa: E402


def all_final(backend, parser: TraceParserService) -> bool:
    open_tasks = backend.store.conn.execute(
        "SELECT COUNT(*) AS n FROM tasks WHERE status NOT IN ('ACCEPTED', 'REJECTED')"
    ).fetchone()["n"]
    return open_tasks == 0 and parser.publish_pending() == 0


def real_adapters(backend):
    """shared/jobs/worker.py's agent adapters as production runs them (artifact fetch and
    register, trial saving, result mapping), over author / world_check with only the
    authoring trial and Docker replaced (fakes.author_* and fakes.docker)."""
    from shared.jobs import worker

    worker._import_task_builder()
    from generator import author  # generator/author.py; draft_fn and world_fn import from it per call
    from shared.taskformat import world_check  # shared/taskformat/world_check.py; gates re-exports its run_world_check_job

    author.run_world_job = fakes.author_world_job
    author.run_create_job = fakes.author_create_job
    world_check._docker = fakes.docker
    generation = worker.generation_fns(backend, mode="agent")
    gates = {"validate": fakes.validate(backend), "world_check": worker.world_check_fn(backend)}
    return generation, gates


def main() -> int:
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(process)d %(name)s %(message)s")
    backend = from_env()
    stop = threading.Event()
    executor = Executor(backend, run_trial=fakes.run_trial)
    parser = TraceParserService(backend, complete=fakes.review)

    # One function per job type; world / world_check are only claimed in agent mode.
    if os.environ.get("SDE_E2E_REAL_ADAPTERS") == "1":
        generation, gates = real_adapters(backend)
    else:
        draft = fakes.generate(backend)
        generation = {"create": draft, "revise": draft, "world": fakes.world(backend)}
        gates = {"validate": fakes.validate(backend), "world_check": fakes.world_check(backend)}
    roles = [
        threading.Thread(target=serve_generation, name="generation", args=(backend, generation),
                         kwargs={"concurrency": 2, "stop": stop}),
        threading.Thread(target=serve_validate, name="validate", args=(backend, gates),
                         kwargs={"concurrency": 2, "docker_slots": DOCKER_SLOTS, "stop": stop}),
        threading.Thread(target=executor.serve, name="executor", kwargs={"n_workers": DOCKER_SLOTS}),
        threading.Thread(target=parser.serve, name="trace-parser", kwargs={"concurrency": 2, "stop": stop}),
        # Sweeps $SDE_ACCEPTED_DIR (this run's), which the Trace Parser publishes to.
        threading.Thread(target=reconciler.run, name="reconciler", args=(backend, stop, 0.5)),
    ]
    for t in roles:
        t.daemon = True
        t.start()

    forever = "--forever" in sys.argv
    while not stop.is_set():
        if not forever and all_final(backend, parser):
            break
        stop.wait(POLL_S)
    stop.set()
    executor.shutdown()
    for t in roles:
        t.join(timeout=10)
    return 0


if __name__ == "__main__":
    sys.exit(main())
