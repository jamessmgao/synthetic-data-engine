"""Build + Validate: task gates (validate) and, in agent mode, world checks, on the generic
loop of shared/jobs/worker.py.

The job functions are `validator/gates.py:run_validate_job` and
`shared/taskformat/world_check.py:run_world_check_job`. The adapters below only translate
between a job row and those functions' arguments and results, and move artifacts between
machines.

    python -m validator.service [validate] [--concurrency 4] [--with-reconciler]
"""

import argparse
import threading
from typing import Any

from shared.contracts.models import AUTHOR_MODE, DOCKER_SLOTS, GATE_TYPES, JobType
from shared.jobs import handoffs
from shared.jobs.jobqueue import JobQueue
from shared.jobs.worker import JobFn, JobFns, dispatch, as_job_fn, run_service, serve
from shared.storage import Backend
from shared.taskformat.leak import leak_review


def serve_validate(backend: Backend, fn: JobFns, **kw: Any) -> None:
    """Build + Validate: task gates (validate) and, in agent mode, world checks.
    Holds a Docker slot per job."""
    types = GATE_TYPES
    serve(JobQueue(backend), types, as_job_fn(fn),
          handoff=dispatch({JobType.VALIDATE: handoffs.after_validate(backend),
                            JobType.WORLD_CHECK: handoffs.after_world_check(backend)}),
          on_start=dispatch({JobType.VALIDATE: handoffs.validate_started(backend),
                             JobType.WORLD_CHECK: handoffs.world_check_started(backend)}), **kw)


# --- adapters to the job functions ----------------------------------------------------


def validate_fn(backend: Backend) -> JobFn:
    from validator.gates import run_validate_job  # validator/gates.py

    def run(job: dict[str, Any]) -> dict[str, Any]:
        sha = job["bundle_sha256"] or job["input"]["bundle_sha256"]
        bundle_dir = backend.artifacts.bundle_path(sha)  # downloads it in S3 mode
        backend.artifacts.fetch_file(leak_review(sha))  # the create job's review, if any: adopted
        trace_dir = backend.artifacts.trace_dir(job["id"])
        out = run_validate_job(bundle_dir, trace_dir, author_checks=(job["input"] or {}).get("author_checks"),
                               bundle_sha256=sha)
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
    from validator.gates import run_world_check_job  # validator/gates.py
    from shared.taskformat.world_check import adopt_author_check

    def run(job: dict[str, Any]) -> dict[str, Any]:
        sha = job["input"]["world_sha256"]
        world_dir = backend.artifacts.world_path(sha)  # downloads it in S3 mode
        # The world author's own stamped check, if it is for this world (SDE_ADOPT_AUTHOR_CHECKS).
        out = (adopt_author_check(job["input"].get("author_check"), world_dir)
               or run_world_check_job(world_dir, backend.artifacts.trace_dir(job["id"])))
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


def start(backend: Backend, args: argparse.Namespace, stop: threading.Event) -> None:
    serve_validate(backend, gate_fns(backend), concurrency=args.concurrency or DOCKER_SLOTS,
                   docker_slots=DOCKER_SLOTS, stop=stop)


def main() -> None:
    run_service("Run Build + Validate.", ["validate"], start)


if __name__ == "__main__":
    main()
