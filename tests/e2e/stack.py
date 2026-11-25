"""
One copy of the whole pipeline in one process, with the fakes from fakes.py in
place of GLM-5.1 (drafting and the failure review), the Harbor gates and Harbor
trials. Everything else is the production code path:

    generator.service.serve_generation   ─┐  claim → heartbeat → run → settle,
      (create, revise; world in agent)   │  each settle handing off to the next
    validator.service.serve_validate     │  stage in the same transaction
      (validate; world_check in agent)   │
    evaluation.executor.Executor         │
    evaluation.trace_parser_service      ─┘  (+ the real classifier and trace parser)
    shared.jobs.reconciler                 dead-letters, re-publishes to Redis

Backends come from the environment exactly as in production (DATABASE_URL,
REDIS_URL, S3_*, SDE_WORK_DIR). The process exits 0 once every task is final and
every accepted one is published, or keeps running if --forever. Kill it at any
point; that's what the tests do.

Usage: python tests/e2e/stack.py [--forever]
"""

import faulthandler
import logging
import os
import signal
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
from generator.service import serve_generation  # noqa: E402
from validator.service import serve_validate  # noqa: E402

import fakes  # noqa: E402


def all_final(backend, parser: TraceParserService) -> bool:
    open_tasks = backend.store.conn.execute(
        "SELECT COUNT(*) AS n FROM tasks WHERE status NOT IN ('ACCEPTED', 'REJECTED')"
    ).fetchone()["n"]
    return open_tasks == 0 and parser.publish_pending() == 0


def real_adapters(backend):
    """The services' agent adapters (generator/service.py, validator/service.py) as production
    runs them (artifact fetch and register, trial saving, result mapping), over author / world_check with only the
    authoring trial and Docker replaced (fakes.author_* and fakes.docker)."""
    from generator import service as generation_service
    from validator import service as validate_service

    from generator import author  # generator/author.py; draft_fn and world_fn import from it per call
    from shared.taskformat import world_check  # shared/taskformat/world_check.py; gates re-exports its run_world_check_job

    author.run_world_job = fakes.author_world_job
    author.run_create_job = fakes.author_create_job
    world_check._docker = fakes.docker
    generation = generation_service.generation_fns(backend, mode="agent")
    gates = {"validate": fakes.validate(backend), "world_check": validate_service.world_check_fn(backend)}
    return generation, gates


def main() -> int:
    # Thread stacks to the stack log on a fatal signal, or on SIGUSR1 (conftest sends it when a
    # wait times out), so a stalled or crashed stack explains itself.
    faulthandler.enable(all_threads=True)
    faulthandler.register(signal.SIGUSR1, all_threads=True)
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(process)d %(name)s %(message)s")
    if os.environ.get("SDE_E2E_EARLY_STOP") != "1":
        # Early stopping decides a version on whichever runs finish first, so the scripted
        # scenarios (exact jobs, exact pass counts) pin it off; one test turns it on.
        from evaluation import trace_parser_service
        trace_parser_service.early_stop = lambda scored, n_runs: None
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
