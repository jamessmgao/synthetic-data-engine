"""
Read models behind the API (docs/PRD.md §4): batch status, the task verdict and
the batch report, computed from the `pipeline` store. Submission goes through
`JobService.submit_batch`; nothing here writes.

What workers write and these read (see job_service/service.py):
  - generate: `input.origin` in {initial, repair, revision}, optional
    `input.operator`; on success `outcome=OK` and `bundle_sha256`.
  - validate: `result.gates` = {name: {"passed": bool, ...}}; `outcome=OK`
    only if every gate passed.
  - rollout: `slot` 1..N, `outcome` VALID_PASS | VALID_FAIL | INVALID.
"""

from collections import Counter, defaultdict
from datetime import datetime
from typing import Any

from shared.storage.sql import SqlStore, row_dict
from shared.contracts.models import SCORED_OUTCOMES, JobStatus, JobType, Outcome, Stage, TaskStatus

from job_service.calibration import eval_config, in_band
from job_service.service import decision_text
from job_service.verdict import pass_pattern


class NotFound(Exception):
    pass


def _batch_rows(store: SqlStore, batch_id: str) -> tuple[list[dict], dict[str, list[dict]]]:
    """The batch's tasks (in id order) and their jobs grouped by task id."""
    tasks = store.batch_tasks(batch_id)
    if not tasks:
        raise NotFound(f"batch {batch_id!r} not found")
    jobs_by_task: dict[str, list[dict]] = defaultdict(list)
    rows = store.conn.execute(
        "SELECT j.* FROM jobs j JOIN tasks t ON t.id = j.task_id"
        " WHERE t.batch_id = ? ORDER BY j.created_at, j.id",
        (batch_id,),
    ).fetchall()
    for job in map(row_dict, rows):
        jobs_by_task[job["task_id"]].append(job)
    return tasks, jobs_by_task


def _scored(jobs: list[dict], version: int) -> dict[int, str]:
    """slot -> VALID_PASS / VALID_FAIL for one version."""
    return {
        j["slot"]: j["outcome"]
        for j in jobs
        if j["type"] == JobType.ROLLOUT and j["version"] == version and j["outcome"] in SCORED_OUTCOMES
    }


def _sum_cost(jobs) -> float | None:
    """Sum of recorded costs; None if nothing has a recorded cost yet."""
    costs = [j["cost_usd"] for j in jobs if j["cost_usd"] is not None]
    return round(sum(costs), 6) if costs else None


def _gate_passed(gate: Any) -> bool:
    return gate is True or (isinstance(gate, dict) and gate.get("passed") is True)


def list_batches(store: SqlStore) -> list[dict]:
    rows = store.conn.execute(
        """SELECT batch_id, COUNT(*) AS tasks, MIN(created_at) AS created_at,
                  COUNT(*) FILTER (WHERE status = 'ACCEPTED') AS accepted,
                  COUNT(*) FILTER (WHERE status = 'REJECTED') AS rejected
             FROM tasks GROUP BY batch_id ORDER BY MIN(created_at) DESC"""
    ).fetchall()
    return [dict(r) for r in rows]


def get_batch(store: SqlStore, batch_id: str) -> dict:
    tasks, jobs_by_task = _batch_rows(store, batch_id)
    n_runs = eval_config()["n_runs"]
    return {
        "batch_id": batch_id,
        "counts_by_status": dict(Counter(t["status"] for t in tasks)),
        "tasks": [
            {
                "id": t["id"],
                "name": t["name"],
                "status": t["status"],
                "reason": t["reason"],
                "current_version": t["current_version"],
                "stage": t["stage"],
                "pass_pattern": pass_pattern(jobs_by_task[t["id"]], t["current_version"], n_runs),
                "cost_usd": _sum_cost(jobs_by_task[t["id"]]),
            }
            for t in tasks
        ],
    }


def _version_view(task: dict, version: int, jobs: list[dict], n_runs: int) -> dict:
    vjobs = [j for j in jobs if j["version"] == version]
    generates = [j for j in vjobs if j["type"] == JobType.GENERATE]
    validates = [j for j in vjobs if j["type"] == JobType.VALIDATE]
    scored = _scored(jobs, version)
    pass_count = sum(o == Outcome.VALID_PASS for o in scored.values())

    # The version's origin is its first non-repair generate job.
    first = next((j for j in generates if (j["input"] or {}).get("origin") != "repair"), None)
    first_input = (first or {}).get("input") or {}
    built = [j for j in generates if j["outcome"] == Outcome.OK]

    if version == task["current_version"]:
        state = task["stage"]
    elif len(scored) == n_runs:
        # Only an out-of-band version is ever superseded by a revision.
        state = Stage.BAND_OK if in_band(pass_count) else Stage.OUT_OF_BAND
    else:
        state = Stage.INVALID

    return {
        "n": version,
        "origin": first_input.get("origin"),
        "operator": first_input.get("operator"),
        "state": state,
        "bundle_sha256": built[-1]["bundle_sha256"] if built else None,
        "repairs": sum((j["input"] or {}).get("origin") == "repair" for j in generates),
        "gates": (validates[-1]["result"] or {}).get("gates") if validates else None,
        "pass_count": pass_count if len(scored) == n_runs else None,
        "pass_pattern": pass_pattern(jobs, version, n_runs),
        "cost_usd": _sum_cost(vjobs),
    }


def get_task(store: SqlStore, task_id: str) -> dict:
    """The verdict for one task: every version, gate, rollout and the decision."""
    task = store.task(task_id)
    if task is None:
        raise NotFound(f"task {task_id!r} not found")
    jobs = store.task_jobs(task_id)
    n_runs = eval_config()["n_runs"]
    rollouts = sorted(
        (j for j in jobs if j["type"] == JobType.ROLLOUT),
        key=lambda j: (j["version"], j["slot"], j["retry"]),
    )
    return {
        "task": {k: task[k] for k in (
            "id", "batch_id", "name", "slot", "status", "reason", "current_version", "stage",
            "accepted_version", "created_at", "updated_at",
        )},
        "versions": [_version_view(task, v, jobs, n_runs) for v in range(1, task["current_version"] + 1)],
        "rollouts": [
            {k: j[k] for k in (
                "id", "version", "slot", "retry", "status", "outcome", "reward", "error",
                "trace_uri", "trace_metrics", "failure", "cost_usd",
            )}
            for j in rollouts
        ],
        "decision": decision_text(task, jobs),
        "reason": task["reason"],
        "cost_usd": _sum_cost(jobs),
    }


def batch_report(store: SqlStore, batch_id: str) -> dict:
    """summary.json (docs/PRD.md §4). Counts are over initial candidates (tasks),
    not versions; `null` means not measured yet, never zero."""
    tasks, jobs_by_task = _batch_rows(store, batch_id)
    all_jobs = [j for t in tasks for j in jobs_by_task[t["id"]]]
    cfg = eval_config()

    def validates(task_id):
        return [j for j in jobs_by_task[task_id] if j["type"] == JobType.VALIDATE and j["status"] == JobStatus.DONE]

    histogram = {str(n): 0 for n in range(cfg["n_runs"] + 1)}
    for t in tasks:
        for v in range(1, t["current_version"] + 1):
            scored = _scored(jobs_by_task[t["id"]], v)
            if len(scored) == cfg["n_runs"]:
                histogram[str(sum(o == Outcome.VALID_PASS for o in scored.values()))] += 1

    rollouts = [j for j in all_jobs if j["type"] == JobType.ROLLOUT]
    authoring = _sum_cost(j for j in all_jobs if j["type"] != JobType.ROLLOUT)
    evaluation = _sum_cost(rollouts)
    total = None if authoring is None and evaluation is None else round((authoring or 0) + (evaluation or 0), 6)
    accepted = [t for t in tasks if t["status"] == TaskStatus.ACCEPTED]

    finished = [j["finished_at"] for j in all_jobs if j["finished_at"]]
    started = min(t["created_at"] for t in tasks)
    wall_clock = (
        round((datetime.fromisoformat(max(finished)) - datetime.fromisoformat(started)).total_seconds())
        if finished
        else None
    )
    # More than one config hash means rollouts weren't all measured the same way.
    hashes = sorted({j["config_hash"] for j in rollouts if j["config_hash"]})

    return {
        "batch_id": batch_id,
        "candidates": len(tasks),
        "structural_pass": sum(
            any(_gate_passed(((j["result"] or {}).get("gates") or {}).get("structural")) for j in validates(t["id"]))
            for t in tasks
        ),
        "functional_pass": sum(any(j["outcome"] == Outcome.OK for j in validates(t["id"])) for t in tasks),
        "accepted_v1": sum(t["accepted_version"] == 1 for t in accepted),
        "accepted_total": len(accepted),
        "rejected": dict(Counter(t["reason"] for t in tasks if t["status"] == TaskStatus.REJECTED)),
        "in_progress": sum(t["status"] in (TaskStatus.CREATED, TaskStatus.CALIBRATING) for t in tasks),
        "pass_count_histogram": histogram,
        "valid_rollouts": sum(j["outcome"] in SCORED_OUTCOMES for j in rollouts),
        "invalid_rollouts": sum(j["outcome"] == Outcome.INVALID or j["status"] == JobStatus.DEAD for j in rollouts),
        "cost_usd": {
            "authoring": authoring,
            "evaluation": evaluation,
            "total": total,
            "per_accepted": round(total / len(accepted), 6) if accepted and total is not None else None,
        },
        "wall_clock_s": wall_clock,
        "config_hash": hashes[0] if len(hashes) == 1 else (hashes or None),
    }
