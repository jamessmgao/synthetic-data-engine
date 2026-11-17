"""Worker loops for the generate and validate queues (the executor has its own).

`serve()` is the generic loop: claim a job, keep its lease alive with a heartbeat
thread, run a job function, settle the result. If the lease is lost mid-run, the
result is dropped: the settle is fenced, so it changes nothing. If the function
raises, the job is released with a backoff, and the reconciler dead-letters it
after MAX_JOB_ATTEMPTS claims.

The job functions come from task-builder: `generator/worker.py:run_generate_job`
and `validator/gates.py:run_validate_job`. The adapters below only translate
between a job row and those functions' arguments and results.

    python -m generator.service generate [--concurrency 2] [--with-reconciler]
    python -m validator.service validate [--concurrency 4]
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

from shared.storage import Backend, describe, from_env
from shared.jobs.jobqueue import JobQueue
from shared.contracts.models import DOCKER_SLOTS, HEARTBEAT_S, LEASE_TTL_S, POLL_S, JobType
from shared.jobs import reconciler

log = logging.getLogger("worker")

JobFn = Callable[[dict[str, Any]], dict[str, Any]]
FAILURE_BACKOFF_S = 60.0


def run_one(jq: JobQueue, job: dict[str, Any], fn: JobFn, lease_sec: float = LEASE_TTL_S,
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
        fields = fn(job)
    except Exception as e:
        log.exception("%s: job function raised", job_id)
        jq.release(job_id, token, error=f"{type(e).__name__}: {str(e)[:500]}",
                   delay_sec=FAILURE_BACKOFF_S)
        return False
    finally:
        done.set()
    if lost.is_set() or not jq.settle(job_id, token, fields):
        log.warning("%s: lease lost before settling; result dropped", job_id)
        return False
    log.info("%s: %s%s", job_id, fields.get("outcome"),
             f" ({fields['error'][:200]})" if fields.get("error") else "")
    return True


def serve(
    jq: JobQueue,
    job_type: JobType,
    fn: JobFn,
    *,
    concurrency: int = 1,
    docker_slots: int | None = None,
    stop: threading.Event | None = None,
    batch_id: str | None = None,
) -> None:
    """Run `concurrency` claim loops until `stop` is set."""
    stop = stop or threading.Event()
    base = f"{socket.gethostname()}:{os.getpid()}:{job_type}"

    def loop(owner: str) -> None:
        while not stop.is_set():
            job = jq.claim(job_type, owner, LEASE_TTL_S, docker_slots, batch_id=batch_id)
            if job is None:
                stop.wait(POLL_S)
                continue
            run_one(jq, job, fn)

    threads = [threading.Thread(target=loop, args=(f"{base}:{i}",), name=f"{job_type}-{i}")
               for i in range(concurrency)]
    for t in threads:
        t.start()
    for t in threads:
        while t.is_alive():
            t.join(timeout=0.5)


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
    parser = argparse.ArgumentParser(description="Run generate or validate workers.")
    parser.add_argument("role", choices=["generate", "validate"])
    parser.add_argument("--concurrency", type=int, default=None)
    parser.add_argument("--batch", default=None, help="only claim this batch's jobs")
    parser.add_argument("--with-reconciler", action="store_true",
                        help="also run the reconciler in this process (worker-control)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(threadName)s %(levelname)s %(message)s")

    backend = from_env()
    log.info("backends: %s", describe(backend))
    jq = JobQueue(backend)
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    if args.with_reconciler:
        reconciler.start_thread(backend)

    if args.role == "generate":
        serve(jq, JobType.GENERATE, generate_fn(backend), concurrency=args.concurrency or 2,
              stop=stop, batch_id=args.batch)
    else:
        serve(jq, JobType.VALIDATE, validate_fn(backend),
              concurrency=args.concurrency or DOCKER_SLOTS, docker_slots=DOCKER_SLOTS,
              stop=stop, batch_id=args.batch)


if __name__ == "__main__":
    main()
