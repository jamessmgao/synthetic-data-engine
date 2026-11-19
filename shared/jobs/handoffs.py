"""
Stage hand-offs for the Generation Service and Build + Validate (PRD §5.1, §5.2).

Each service settles its job and, in the same transaction, moves the task to the
next stage and enqueues the next stage's jobs. These functions are that part.
They hold the pipeline's rules, so the e2e suite runs them with fake job
functions plugged in front of them.

    create / revise / repair<k> ─OK─► validate-r<k> ─OK─► 5 rollouts on the run queue
          │FAILED                        │FAILED
          ▼                              ▼
     repair<k+1> if the author    repair<k+1> on the revise queue (≤ MAX_REPAIRS_PER_VERSION),
     wrote a draft that needs     else REJECTED invalid
     fixing (≤ MAX_REPAIRS…),
     else REJECTED invalid

In agent mode a version starts one step earlier, with its world
(docs/AUTHORING.md §1):

    world / world-repair<k> ─OK─► world-check-r<k> ─OK─► create (from the world) ─► …as above
          │FAILED                        │FAILED
          ▼                              ▼
     …-retry<r+1>, same input  world-repair<k+1> (≤ MAX_REPAIRS_PER_VERSION), else REJECTED invalid
     (≤ MAX_REPAIRS_PER_VERSION),
     else REJECTED invalid

A world run fails for reasons that aren't the task's fault (the agent timed out, the
gateway broke, the agent ended without a world), so a failed world job is retried
before the task is given up; result.failure_kind says infra or author.

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
_WORLD_REPAIR = re.compile(r"-world-repair(\d+)(?:-retry\d+)?$")
_RETRY = re.compile(r"-retry(\d+)$")


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
    """A draft settled: validate it. A draft the author got wrong but wrote (e.g. its
    instruction leaks the fix: the create job's leak review, plan N6) is repaired like a
    failed gate. Otherwise reject a task that couldn't be drafted."""

    def handoff(conn: Conn, job: dict[str, Any]) -> list[dict[str, Any]]:
        tid, n = job["task_id"], job["version"]
        if job["outcome"] != Outcome.OK:
            res, k = job["result"] or {}, draft_round(job)
            sha = job.get("bundle_sha256") or res.get("bundle_sha256")
            if res.get("failure_kind") == "author" and sha and k < MAX_REPAIRS_PER_VERSION:
                if not tasks.cas(conn, tid, version=n, stages=(Stage.QUEUED, Stage.GENERATING),
                                 stage=Stage.GENERATING):
                    return []
                world = res.get("world_sha256") or (job["input"] or {}).get("world_sha256")
                return [{
                    "id": job_id(tid, n, "repair", k + 1), "type": JobType.REVISE, "task_id": tid, "version": n,
                    "input": {"origin": "repair", "bundle_sha256": sha,
                              "feedback": res.get("feedback") or job["error"] or "the draft was rejected",
                              **({"world_sha256": world} if world else {})},
                }]
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
        # A task written from a world keeps it through validate, so its repairs are too.
        world = (job["result"] or {}).get("world_sha256") or (job["input"] or {}).get("world_sha256")
        # The create job's stamped self-check runs, which validate may adopt.
        checked = (job["result"] or {}).get("self_check") or {}
        author = ({"job_id": job["id"], "bundle_sha256": sha, "stamp": checked.get("stamp"),
                   "roles": checked["roles"]} if checked.get("roles") else None)
        return [{
            "id": job_id(tid, n, "validate", k), "type": JobType.VALIDATE, "task_id": tid, "version": n,
            "bundle_sha256": sha,
            "input": {"bundle_sha256": sha, "bundle_dir": str(backend.artifacts.bundle_path(sha)), "round": k,
                      **({"world_sha256": world} if world else {}),
                      **({"author_checks": author} if author else {})},
        }]

    return handoff


# --- Worlds (agent mode) ---------------------------------------------------------------


def world_round(job: dict[str, Any]) -> int:
    """Which world draft of its version a world job is: 0, or k for world-repair<k>."""
    m = _WORLD_REPAIR.search(job["id"])
    return int(m.group(1)) if m else 0


def world_retry(job: dict[str, Any]) -> int:
    """Which attempt at the same world job this is: 0, or r for …-retry<r>."""
    m = _RETRY.search(job["id"])
    return int(m.group(1)) if m else 0


def world_started(backend: Backend):
    def start(job: dict[str, Any]) -> None:
        with backend.store.tx() as conn:
            tasks.start(conn, job["task_id"], job["version"], Stage.WORLD_BUILDING,
                        (Stage.QUEUED, Stage.WORLD_BUILDING))

    return start


def after_world(backend: Backend):
    """A world settled: check it, or reject a task whose world couldn't be built."""

    def handoff(conn: Conn, job: dict[str, Any]) -> list[dict[str, Any]]:
        tid, n = job["task_id"], job["version"]
        live = (Stage.QUEUED, Stage.WORLD_BUILDING)
        sha = (job["result"] or {}).get("world_sha256")
        if job["outcome"] != Outcome.OK:
            r = world_retry(job)
            if r >= MAX_REPAIRS_PER_VERSION:
                tasks.reject(conn, tid, RejectReason.INVALID, Stage.INVALID, version=n, stages=live)
                return []
            if not tasks.cas(conn, tid, version=n, stages=live, stage=Stage.WORLD_BUILDING):
                return []
            base = _RETRY.sub("", job["id"])
            return [{"id": f"{base}-retry{r + 1}", "type": JobType.WORLD, "task_id": tid, "version": n,
                     "input": job["input"] or {}}]
        if not sha:  # reported OK but produced no world: nothing to check, and nothing would re-queue it
            tasks.reject(conn, tid, RejectReason.INVALID, Stage.INVALID, version=n, stages=live)
            return []
        if not tasks.cas(conn, tid, version=n, stages=live, stage=Stage.WORLD_CHECKING):
            return []
        k = world_round(job)
        # The world job's stamped self-check, which the world_check job may adopt.
        checked = ((job["result"] or {}).get("authoring") or {}).get("self_check") or {}
        author = ({"job_id": job["id"], "stamp": checked.get("stamp"), "result": checked["result"]}
                  if checked.get("result") else None)
        return [{
            "id": job_id(tid, n, "world-check", k), "type": JobType.WORLD_CHECK, "task_id": tid,
            "version": n,
            "input": {"world_sha256": sha, "world_dir": str(backend.artifacts.world_path(sha)),
                      "spec": (job["input"] or {}).get("spec"), "round": k,
                      **({"author_check": author} if author else {}), **_variant_of(job)},
        }]

    return handoff


def world_check_started(backend: Backend):
    def start(job: dict[str, Any]) -> None:
        with backend.store.tx() as conn:
            tasks.start(conn, job["task_id"], job["version"], Stage.WORLD_CHECKING,
                        (Stage.WORLD_CHECKING,))

    return start


def after_world_check(backend: Backend):
    """The world checked out: author a task from it. Otherwise repair the world."""

    def handoff(conn: Conn, job: dict[str, Any]) -> list[dict[str, Any]]:
        tid, n = job["task_id"], job["version"]
        inp = job["input"] or {}
        sha, spec, k = inp["world_sha256"], inp.get("spec") or {}, int(inp.get("round", 0))
        live = (Stage.WORLD_CHECKING,)
        if job["outcome"] == Outcome.OK:
            if not tasks.cas(conn, tid, version=n, stages=live, stage=Stage.QUEUED):
                return []
            return [{
                "id": job_id(tid, n, "create"), "type": JobType.CREATE, "task_id": tid, "version": n,
                "input": {"origin": "initial", "world_sha256": sha, "shape": spec.get("shape"),
                          "dial": spec.get("dial"), **_variant_of(job)},
            }]
        if k >= MAX_REPAIRS_PER_VERSION:
            tasks.reject(conn, tid, RejectReason.INVALID, Stage.INVALID, version=n, stages=live)
            return []
        if not tasks.cas(conn, tid, version=n, stages=live, stage=Stage.WORLD_BUILDING):
            return []
        feedback = (job["result"] or {}).get("feedback") or job["error"] or "the world check failed"
        return [{
            "id": job_id(tid, n, "world-repair", k + 1), "type": JobType.WORLD, "task_id": tid,
            "version": n,
            "input": {"origin": "repair", "world_sha256": sha, "feedback": feedback, "spec": spec,
                      **_variant_of(job)},
        }]

    return handoff


def _variant_of(job: dict[str, Any]) -> dict[str, Any]:
    """A variant world's source (a reused world): carried world → world_check → create, and
    through world repairs, for lineage and the earlier tasks its create must not repeat."""
    source = (job["input"] or {}).get("variant_of")
    return {"variant_of": source} if source else {}


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
        world = (job["input"] or {}).get("world_sha256")
        return [{
            "id": job_id(tid, n, "repair", k + 1), "type": JobType.REVISE, "task_id": tid, "version": n,
            "input": {"origin": "repair", "bundle_sha256": sha, "feedback": feedback,
                      **({"world_sha256": world} if world else {})},
        }]

    return handoff
