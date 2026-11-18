"""
Stage hand-offs for the Generation Service and Build + Validate (PRD §5.1, §5.2).

Each service settles its job and, in the same transaction, moves the task to the
next stage and enqueues the next stage's jobs. These functions are that part.
They hold the pipeline's rules, so the e2e suite runs them with fake job
functions plugged in front of them.

    create / revise / repair<k> ─OK─► validate-r<k> ─OK─► 5 rollouts on the run queue
          │FAILED                        │FAILED
          ▼                              ▼
     REJECTED invalid        repair<k+1> on the revise queue (≤ MAX_REPAIRS_PER_VERSION),
                             else REJECTED invalid

Every task change is a compare-and-set on (current_version, stage): if the task
moved on (decided, or a copy got there first), nothing is enqueued.
"""

import re
from typing import Any

from evaluation.calibration import config_hash, eval_config
from shared.jobs import tasks
from shared.storage import Backend
from shared.storage.sql import Conn, job_id, rollout_job_id
from shared.contracts.models import MAX_REPAIRS_PER_VERSION, JobType, Outcome, RejectReason, Stage

_REPAIR = re.compile(r"-repair(\d+)$")


def draft_round(job: dict[str, Any]) -> int:
    """Which draft of its version a create / revise job is: 0, or k for repair<k>."""
    m = _REPAIR.search(job["id"])
    return int(m.group(1)) if m else 0


# --- Generation Service -------------------------------------------------------------


def generation_started(backend: Backend):
    def start(job: dict[str, Any]) -> None:
        with backend.store.tx() as conn:
            tasks.start(conn, job["task_id"], job["version"], Stage.GENERATING,
                        (Stage.QUEUED, Stage.GENERATING))

    return start


def after_generation(backend: Backend):
    """A draft settled: validate it, or reject a task that couldn't be drafted."""

    def handoff(conn: Conn, job: dict[str, Any]) -> list[dict[str, Any]]:
        tid, n = job["task_id"], job["version"]
        if job["outcome"] != Outcome.OK:
            tasks.reject(conn, tid, RejectReason.INVALID, Stage.INVALID, version=n)
            return []
        sha = job["bundle_sha256"] or (job["result"] or {}).get("bundle_sha256")
        name = (job["result"] or {}).get("name")
        k = draft_round(job)
        fields = {"bundle_sha256": sha, "stage": Stage.BUILDING}
        if name:
            fields["name"] = name
        if not tasks.cas(conn, tid, version=n, stages=(Stage.QUEUED, Stage.GENERATING), **fields):
            return []
        return [{
            "id": job_id(tid, n, "validate", k), "type": JobType.VALIDATE, "task_id": tid, "version": n,
            "bundle_sha256": sha,
            "input": {"bundle_sha256": sha, "bundle_dir": str(backend.artifacts.bundle_path(sha)), "round": k},
        }]

    return handoff


# --- Build + Validate ---------------------------------------------------------------


def validate_started(backend: Backend):
    def start(job: dict[str, Any]) -> None:
        with backend.store.tx() as conn:
            tasks.start(conn, job["task_id"], job["version"], Stage.VALIDATING,
                        (Stage.BUILDING, Stage.VALIDATING))

    return start


def after_validate(backend: Backend):
    """Gates settled: five rollouts on the run queue, or a repair on the revise queue."""
    n_runs = eval_config()["n_runs"]

    def handoff(conn: Conn, job: dict[str, Any]) -> list[dict[str, Any]]:
        tid, n = job["task_id"], job["version"]
        sha = job["bundle_sha256"] or (job["input"] or {}).get("bundle_sha256")
        k = int((job["input"] or {}).get("round", job["id"].rsplit("-r", 1)[1]))
        live = (Stage.BUILDING, Stage.VALIDATING)
        if job["outcome"] == Outcome.OK:
            if not tasks.cas(conn, tid, version=n, stages=live, stage=Stage.EVALUATING):
                return []
            task_dir = str(backend.artifacts.bundle_path(sha))
            return [{
                "id": rollout_job_id(tid, n, slot, 0), "type": JobType.ROLLOUT, "task_id": tid, "version": n,
                "slot": slot, "retry": 0, "bundle_sha256": sha, "config_hash": config_hash(),
                "input": {"task_dir": task_dir},
            } for slot in range(1, n_runs + 1)]
        if k >= MAX_REPAIRS_PER_VERSION:
            tasks.reject(conn, tid, RejectReason.INVALID, Stage.INVALID, version=n, stages=live)
            return []
        if not tasks.cas(conn, tid, version=n, stages=live, stage=Stage.GENERATING):
            return []
        feedback = (job["result"] or {}).get("feedback") or job["error"] or "a gate failed"
        return [{
            "id": job_id(tid, n, "repair", k + 1), "type": JobType.REVISE, "task_id": tid, "version": n,
            "input": {"origin": "repair", "bundle_sha256": sha, "feedback": feedback},
        }]

    return handoff
