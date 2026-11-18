"""Service loops for the Generation Service and Build + Validate (the Executor and
the Trace Parser have their own).

`serve()` is the generic loop: claim a job, mark the task's stage (`on_start`),
keep the lease alive with a heartbeat thread, run a job function, settle the
result together with the hand-off to the next stage (`handoff`, run inside the
settle transaction, see shared/jobs/handoffs.py). If the lease is lost mid-run, the
result is dropped: the settle is fenced, so it changes nothing. If the function
raises, the job is released with a backoff, and the reconciler dead-letters it
after MAX_JOB_ATTEMPTS claims.

The job functions come from task-builder: `generator/worker.py:run_generate_job`
and `validator/gates.py:run_validate_job`. The adapters below only translate
between a job row and those functions' arguments and results.

    python -m generator.service generation [--concurrency 2] [--with-reconciler]
    python -m validator.service validate [--concurrency 4]

The Generation Service claims from the revise queue before the create queue, so
started tasks finish before new ones are drafted. (`generate` is accepted as an
alias for `generation`.)
"""

import argparse
import logging
import os
import signal
import socket
import sys
import threading
from collections.abc import Callable
from typing import Any

from shared.jobs import handoffs, reconciler
from shared.storage import Backend, describe, from_env
from shared.jobs.jobqueue import Handoff, JobQueue
from shared.contracts.models import DOCKER_SLOTS, GENERATION_TYPES, HEARTBEAT_S, LEASE_TTL_S, POLL_S, JobType

log = logging.getLogger("worker")

JobFn = Callable[[dict[str, Any]], dict[str, Any]]
OnStart = Callable[[dict[str, Any]], None]
FAILURE_BACKOFF_S = 60.0


def run_one(jq: JobQueue, job: dict[str, Any], fn: JobFn, handoff: Handoff | None = None,
            on_start: OnStart | None = None, lease_sec: float = LEASE_TTL_S,
            heartbeat_sec: float = HEARTBEAT_S) -> bool:
    """Run one claimed job to a settle (or release). Returns True if it was settled."""
    job_id, token = job["id"], job["lease_token"]
    done = threading.Event()
    lost = threading.Event()

    def beat() -> None:
        while not done.wait(heartbeat_sec):
            if not jq.heartbeat(job_id, token, lease_sec):
                lost.set()
                return

    threading.Thread(target=beat, name=f"heartbeat-{job_id}", daemon=True).start()
    try:
        if on_start is not None:
            on_start(job)
        fields = fn(job)
    except Exception as e:
        log.exception("%s: job function raised", job_id)
        jq.release(job_id, token, error=f"{type(e).__name__}: {str(e)[:500]}",
                   delay_sec=FAILURE_BACKOFF_S)
        return False
    finally:
        done.set()
    if lost.is_set() or not jq.settle(job_id, token, fields, then=handoff):
        log.warning("%s: lease lost before settling; result dropped", job_id)
        return False
    log.info("%s: %s%s", job_id, fields.get("outcome"),
             f" ({fields['error'][:200]})" if fields.get("error") else "")
    return True


def serve(
    jq: JobQueue,
    job_types: JobType | tuple[JobType, ...] | list[JobType],
    fn: JobFn,
    *,
    handoff: Handoff | None = None,
    on_start: OnStart | None = None,
    concurrency: int = 1,
    docker_slots: int | None = None,
    stop: threading.Event | None = None,
) -> None:
    """Run `concurrency` claim loops until `stop` is set."""
    stop = stop or threading.Event()
    types = [job_types] if isinstance(job_types, str) else list(job_types)
    name = "+".join(map(str, types))
    base = f"{socket.gethostname()}:{os.getpid()}:{name}"

    def loop(owner: str) -> None:
        while not stop.is_set():
            job = jq.claim(types, owner, LEASE_TTL_S, docker_slots)
            if job is None:
                stop.wait(POLL_S)
                continue
            run_one(jq, job, fn, handoff, on_start)

    threads = [threading.Thread(target=loop, args=(f"{base}:{i}",), name=f"{name}-{i}")
               for i in range(concurrency)]
    for t in threads:
        t.start()
    for t in threads:
        while t.is_alive():
            t.join(timeout=0.5)


# --- the two services ---------------------------------------------------------------


def serve_generation(backend: Backend, fn: JobFn, **kw: Any) -> None:
    """The Generation Service: the revise queue, then the create queue."""
    serve(JobQueue(backend), GENERATION_TYPES, fn, handoff=handoffs.after_generation(backend),
          on_start=handoffs.generation_started(backend), **kw)


def serve_validate(backend: Backend, fn: JobFn, **kw: Any) -> None:
    """Build + Validate: holds a Docker slot per job."""
    serve(JobQueue(backend), JobType.VALIDATE, fn, handoff=handoffs.after_validate(backend),
          on_start=handoffs.validate_started(backend), **kw)


# --- adapters to task-builder's job functions -------------------------------------


def _import_task_builder():
    """task-builder's modules use flat imports (`import config`), so they need
    generator/ and validator/ on the path."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for sub in ("generator", "validator"):
        path = os.path.join(root, sub)
        if path not in sys.path:
            sys.path.insert(0, path)


def generate_fn(backend: Backend) -> JobFn:
    _import_task_builder()
    from generator.worker import run_generate_job  # generator/worker.py

    def run(job: dict[str, Any]) -> dict[str, Any]:
        out = run_generate_job(job["id"], job["input"], backend.work_dir)
        fields = {k: out.get(k) for k in ("outcome", "error", "result", "cost_usd")}
        if sha := (out.get("result") or {}).get("bundle_sha256"):
            backend.artifacts.register_bundle(sha)
            fields["bundle_sha256"] = sha
        return fields

    return run


def validate_fn(backend: Backend) -> JobFn:
    _import_task_builder()
    from validator.gates import run_validate_job  # validator/gates.py

    def run(job: dict[str, Any]) -> dict[str, Any]:
        sha = job["bundle_sha256"] or job["input"]["bundle_sha256"]
        bundle_dir = backend.artifacts.bundle_path(sha)  # downloads it in S3 mode
        trace_dir = backend.artifacts.trace_dir(job["id"])
        out = run_validate_job(bundle_dir, trace_dir)
        result = {**out["result"], "feedback": out.get("feedback", ""),
                  "failed_gate": out.get("failed_gate")}
        return {
            "outcome": out["outcome"],
            "result": result,
            "error": out.get("feedback") or None,
            "bundle_sha256": sha,
            "trace_uri": backend.artifacts.save_trace(job["id"]),
        }

    return run


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Generation Service or Build + Validate.")
    parser.add_argument("role", choices=["generation", "generate", "validate"])
    parser.add_argument("--concurrency", type=int, default=None)
    parser.add_argument("--with-reconciler", action="store_true",
                        help="also run the reconciler in this process (worker-control)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(threadName)s %(levelname)s %(message)s")

    backend = from_env()
    log.info("backends: %s", describe(backend))
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    if args.with_reconciler:
        reconciler.start_thread(backend)

    if args.role in ("generation", "generate"):
        serve_generation(backend, generate_fn(backend), concurrency=args.concurrency or 2,
                         stop=stop)
    else:
        serve_validate(backend, validate_fn(backend), concurrency=args.concurrency or DOCKER_SLOTS,
                       docker_slots=DOCKER_SLOTS, stop=stop)


if __name__ == "__main__":
    main()
