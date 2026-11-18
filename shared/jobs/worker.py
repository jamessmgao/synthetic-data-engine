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
(SDE_AUTHOR_MODE=oneshot), or `generator/author.py:run_world_job / run_create_job`
(agent), and `validator/gates.py:run_validate_job / run_world_check_job`. The adapters below only translate between a
job row and those functions' arguments and results, and move artifacts between
machines.

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
import time
from collections.abc import Callable
from typing import Any

from shared.jobs import handoffs, reconciler
from shared.storage import Backend, describe, from_env
from shared.jobs.jobqueue import Handoff, JobQueue
from shared.contracts.models import (
    AGENT_GENERATION_TYPES,
    AGENT_SLOTS,
    AUTHOR_MODE,
    DOCKER_SLOTS,
    GATE_TYPES,
    GENERATION_TYPES,
    HEARTBEAT_S,
    LEASE_TTL_S,
    POLL_S,
    JobType,
)

log = logging.getLogger("worker")

JobFn = Callable[[dict[str, Any]], dict[str, Any]]
OnStart = Callable[[dict[str, Any]], None]
FAILURE_BACKOFF_S = 60.0
HEARTBEAT_RETRY_S = 5.0  # after a failed heartbeat, retry sooner than the normal interval
# Agent jobs are hour-long authoring trials: a lease long enough to ride out a database
# outage, so a transient error never makes another worker re-run (and re-pay) one.
AGENT_LEASE_TTL_S = float(os.environ.get("SDE_AGENT_LEASE_TTL_S", "1800"))


def run_one(jq: JobQueue, job: dict[str, Any], fn: JobFn, handoff: Handoff | None = None,
            on_start: OnStart | None = None, lease_sec: float = LEASE_TTL_S,
            heartbeat_sec: float = HEARTBEAT_S) -> bool:
    """Run one claimed job to a settle (or release). Returns True if it was settled."""
    job_id, token = job["id"], job["lease_token"]
    done = threading.Event()
    lost = threading.Event()

    def beat() -> None:
        # A heartbeat that raises (a dropped connection, a failover) is retried, never
        # fatal: a dead heartbeat thread lets the lease lapse under a running job, and the
        # job is then run twice. Only a refused heartbeat (another owner holds the lease)
        # means lost; settle is fenced by the lease token either way.
        wait, failing_since = heartbeat_sec, None
        while not done.wait(wait):
            try:
                held = jq.heartbeat(job_id, token, lease_sec)
            except Exception as e:
                failing_since = failing_since or time.monotonic()
                log.warning("%s: heartbeat failed for %.0fs (lease %.0fs): %s: %s", job_id,
                            time.monotonic() - failing_since, lease_sec, type(e).__name__, str(e)[:200])
                wait = min(heartbeat_sec, HEARTBEAT_RETRY_S)
                continue
            wait, failing_since = heartbeat_sec, None
            if not held:
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
    # Settle even when the lease is lost: it is fenced, so it changes nothing then,
    # except that a job closed by a cancel still records what it spent.
    if not jq.settle(job_id, token, fields, then=handoff) or lost.is_set():
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
    agent_slots: int | None = None,
    stop: threading.Event | None = None,
    lease_sec: float = LEASE_TTL_S,
) -> None:
    """Run `concurrency` claim loops until `stop` is set."""
    stop = stop or threading.Event()
    types = [job_types] if isinstance(job_types, str) else list(job_types)
    name = "+".join(map(str, types))
    base = f"{socket.gethostname()}:{os.getpid()}:{name}"

    def loop(owner: str) -> None:
        while not stop.is_set():
            job = jq.claim(types, owner, lease_sec, docker_slots, agent_slots)
            if job is None:
                stop.wait(POLL_S)
                continue
            run_one(jq, job, fn, handoff, on_start, lease_sec=lease_sec)

    threads = [threading.Thread(target=loop, args=(f"{base}:{i}",), name=f"{name}-{i}")
               for i in range(concurrency)]
    for t in threads:
        t.start()
    for t in threads:
        while t.is_alive():
            t.join(timeout=0.5)


# --- the two services ---------------------------------------------------------------
# Each service serves several job types; the job function, the start hook and the
# hand-off are picked per job by its type.

JobFns = JobFn | dict[str, JobFn]


def _dispatch(table: dict[str, Any]) -> Callable[..., Any]:
    """Call the entry for the job's type: f(job) or f(conn, job)."""

    def pick(*args: Any) -> Any:
        job = args[-1]
        return table[job["type"]](*args)

    return pick


def _fns(fn: JobFns) -> JobFn:
    return _dispatch(fn) if isinstance(fn, dict) else fn


def serve_generation(backend: Backend, fn: JobFns, **kw: Any) -> None:
    """The Generation Service: revise, then create, then (agent mode) world jobs. In
    agent mode a world job, and a create / revise from a world, is a Harbor authoring
    trial holding a Docker slot; at most AGENT_SLOTS of the machine's slots go to them,
    so gates and rollouts are never starved. One-shot drafts hold no slot (the slot is
    decided per job: jobs.needs_docker). `fn` is one function for every type, or one
    per type."""
    agent = AUTHOR_MODE == "agent"
    types = AGENT_GENERATION_TYPES if agent else GENERATION_TYPES
    if agent:
        kw.setdefault("docker_slots", DOCKER_SLOTS)
        kw.setdefault("agent_slots", AGENT_SLOTS)
        kw.setdefault("lease_sec", AGENT_LEASE_TTL_S)
    drafts = {"handoff": handoffs.after_generation(backend), "start": handoffs.generation_started(backend)}
    world = {"handoff": handoffs.after_world(backend), "start": handoffs.world_started(backend)}
    per_type = {t: (world if t == JobType.WORLD else drafts) for t in types}
    serve(JobQueue(backend), types, _fns(fn),
          handoff=_dispatch({t: h["handoff"] for t, h in per_type.items()}),
          on_start=_dispatch({t: h["start"] for t, h in per_type.items()}), **kw)


def serve_validate(backend: Backend, fn: JobFns, **kw: Any) -> None:
    """Build + Validate: task gates (validate) and, in agent mode, world checks.
    Holds a Docker slot per job."""
    types = GATE_TYPES
    serve(JobQueue(backend), types, _fns(fn),
          handoff=_dispatch({JobType.VALIDATE: handoffs.after_validate(backend),
                             JobType.WORLD_CHECK: handoffs.after_world_check(backend)}),
          on_start=_dispatch({JobType.VALIDATE: handoffs.validate_started(backend),
                              JobType.WORLD_CHECK: handoffs.world_check_started(backend)}), **kw)


# --- adapters to task-builder's job functions -------------------------------------


def _import_task_builder():
    """task-builder's modules use flat imports (`import config`), so they need
    generator/ and validator/ on the path."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for sub in ("generator", "validator"):
        path = os.path.join(root, sub)
        if path not in sys.path:
            sys.path.insert(0, path)


def _settled(out: dict[str, Any]) -> dict[str, Any]:
    return {k: out.get(k) for k in ("outcome", "error", "result", "cost_usd")}


def leak_review(bundle_sha: str) -> str:
    """Where the leak gate keeps a bundle's review (validator/gates.py leak_review_dir).
    The create job writes it; registered, a validate job on another machine adopts it
    instead of calling the model again."""
    return f"leak_reviews/{bundle_sha}/review.json"


def _save_trial(backend: Backend, job_id: str) -> str | None:
    """An agent job's authoring trial lives in its trace dir: save it, so a machine
    that reclaims or retries the job adopts it (fetched before the run) instead of
    paying for it again, and the audit keeps it. Called in a `finally`: a job whose
    authoring function raised is released and may be retried on another machine."""
    if backend.artifacts.trace_dir(job_id).exists():
        return backend.artifacts.save_trace(job_id)
    return None


def draft_fn(backend: Backend, mode: str = AUTHOR_MODE) -> JobFn:
    """create / revise, chosen per job: a job from a world (input.world_sha256, agent mode)
    runs run_create_job (plan §3.4: run_generate_job's result shape plus world_sha256);
    any other job runs one-shot run_generate_job, even in agent mode. Raw prompts, v1
    specs and tasks in flight when the mode switched have no world.

    A job may run on another machine than the one that wrote its inputs, so the
    parent bundle, its draft record and the world are made local first; the new
    bundle and draft record are registered after."""
    _import_task_builder()
    from generator.worker import run_generate_job  # generator/worker.py

    def run(job: dict[str, Any]) -> dict[str, Any]:
        inp = job["input"] or {}
        agent = mode == "agent" and bool(inp.get("world_sha256"))
        if agent:
            from generator.author import run_create_job as draft  # generator/author.py
        else:
            draft = run_generate_job
        if parent := inp.get("bundle_sha256"):
            backend.artifacts.bundle_path(parent)  # downloads it in S3 mode
            backend.artifacts.fetch_file(f"drafts/{parent}.json")
        if world := inp.get("world_sha256"):
            backend.artifacts.world_path(world)
        if agent:
            backend.artifacts.fetch_trace(job["id"])
        trace_uri = None
        try:
            out = draft(job["id"], inp, backend.work_dir)
        finally:
            if agent:
                trace_uri = _save_trial(backend, job["id"])
        fields = _settled(out)
        if sha := (out.get("result") or {}).get("bundle_sha256"):
            backend.artifacts.register_bundle(sha)
            for rel in (f"drafts/{sha}.json", leak_review(sha)):
                if (backend.work_dir / rel).is_file():
                    backend.artifacts.register_file(rel)
            fields["bundle_sha256"] = sha
        if trace_uri:
            fields["trace_uri"] = trace_uri
        return fields

    return run


def world_fn(backend: Backend) -> JobFn:
    """world (agent mode): build a world, repair one (input.world_sha256) or mutate
    one (input.parent_world_sha256) with run_world_job; register the new world."""
    _import_task_builder()
    from generator.author import run_world_job  # generator/author.py

    def run(job: dict[str, Any]) -> dict[str, Any]:
        inp = job["input"] or {}
        for key in ("world_sha256", "parent_world_sha256"):
            if inp.get(key):
                backend.artifacts.world_path(inp[key])  # downloads it in S3 mode
        backend.artifacts.fetch_trace(job["id"])
        try:
            out = run_world_job(job["id"], inp, backend.work_dir)
        finally:
            trace_uri = _save_trial(backend, job["id"])
        fields = _settled(out)
        if sha := (out.get("result") or {}).get("world_sha256"):
            backend.artifacts.register_world(sha)
        if trace_uri:
            fields["trace_uri"] = trace_uri
        return fields

    return run


def generation_fns(backend: Backend, mode: str = AUTHOR_MODE) -> dict[str, JobFn]:
    drafts = draft_fn(backend, mode)
    fns = {JobType.CREATE: drafts, JobType.REVISE: drafts}
    if mode == "agent":
        fns[JobType.WORLD] = world_fn(backend)
    return fns


def generate_fn(backend: Backend, mode: str = AUTHOR_MODE) -> JobFn:
    """One function for every Generation Service job type (dispatches on the type)."""
    return _dispatch(generation_fns(backend, mode))


def validate_fn(backend: Backend) -> JobFn:
    _import_task_builder()
    from validator.gates import run_validate_job  # validator/gates.py

    def run(job: dict[str, Any]) -> dict[str, Any]:
        sha = job["bundle_sha256"] or job["input"]["bundle_sha256"]
        bundle_dir = backend.artifacts.bundle_path(sha)  # downloads it in S3 mode
        backend.artifacts.fetch_file(leak_review(sha))  # the create job's review, if any: adopted
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
            "cost_usd": out.get("cost_usd"),  # the leak-review gate's model call
        }

    return run


def world_check_fn(backend: Backend) -> JobFn:
    """world_check (agent mode): run_world_check_job on the world (plan §3.5)."""
    _import_task_builder()
    from validator.gates import run_world_check_job  # validator/gates.py

    def run(job: dict[str, Any]) -> dict[str, Any]:
        sha = job["input"]["world_sha256"]
        world_dir = backend.artifacts.world_path(sha)  # downloads it in S3 mode
        out = run_world_check_job(world_dir, backend.artifacts.trace_dir(job["id"]))
        result = {**(out.get("result") or {}), "feedback": out.get("feedback", ""),
                  "failed_check": out.get("failed_check"), "world_sha256": sha}
        return {
            "outcome": out["outcome"],
            "result": result,
            "error": out.get("feedback") or None,
            "trace_uri": backend.artifacts.save_trace(job["id"]),
            "cost_usd": out.get("cost_usd"),
        }

    return run


def gate_fns(backend: Backend, mode: str = AUTHOR_MODE) -> dict[str, JobFn]:
    fns = {JobType.VALIDATE: validate_fn(backend)}
    if mode == "agent":
        fns[JobType.WORLD_CHECK] = world_check_fn(backend)
    return fns


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
        log.info("author mode: %s", AUTHOR_MODE)
        # Threads, not slots: one-shot drafts are model calls (no Docker slot), and agent
        # jobs are capped by the Docker / agent slot counts in the claim itself.
        default = int(os.environ.get("SDE_GENERATION_CONCURRENCY", "8"))
        serve_generation(backend, generation_fns(backend), concurrency=args.concurrency or default,
                         stop=stop)
    else:
        serve_validate(backend, gate_fns(backend), concurrency=args.concurrency or DOCKER_SLOTS,
                       docker_slots=DOCKER_SLOTS, stop=stop)


if __name__ == "__main__":
    main()
