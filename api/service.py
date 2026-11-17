"""
Service functions behind the API (docs/PRD.md §4). The `sde` CLI calls these
directly, so everything here takes a connection and returns plain data.

Contract for what workers write into `jobs` (read here, written elsewhere):
  - generate: `input.origin` in {initial, repair, revision}, optional
    `input.operator`; on success `outcome=OK` and `bundle_sha256`.
  - validate: `result.gates` = {schema: bool, structural: bool, lint: bool,
    oracle: [reward, reward], nop: reward}; `outcome=OK` only if all passed.
  - rollout: `slot` 1..N, `outcome` VALID_PASS | VALID_FAIL | INVALID.
"""

import json
import secrets
import sqlite3
from collections import Counter, defaultdict
from datetime import UTC, datetime

from .config import Settings
from .db import parse_ts, transaction, utcnow
from .models import (
    JOB_PRIORITY,
    SCORED_OUTCOMES,
    JobStatus,
    JobType,
    Outcome,
    Stage,
    TaskStatus,
)


class NotFound(Exception):
    pass


class Conflict(Exception):
    pass


def _json(text: str | None):
    return json.loads(text) if text else None


def new_batch_id() -> str:
    return f"b-{datetime.now(UTC):%Y%m%d}-{secrets.token_hex(3)}"


def task_ids_for(batch_id: str, count: int) -> list[str]:
    return [f"{batch_id}-t{i:02d}" for i in range(1, count + 1)]


def submit_batch(conn: sqlite3.Connection, count: int, batch_id: str | None = None) -> dict:
    """Create `count` tasks and their v1 generate jobs in one transaction.

    Idempotent on `batch_id`: ids are deterministic, so repeating a submission
    inserts nothing. Repeating it with a different count is a Conflict.
    """
    batch_id = batch_id or new_batch_id()
    task_ids = task_ids_for(batch_id, count)
    now = utcnow()
    with transaction(conn):
        existing = conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE batch_id = ?", (batch_id,)
        ).fetchone()[0]
        if existing and existing != count:
            raise Conflict(f"batch {batch_id!r} already exists with {existing} tasks, not {count}")
        for task_id in task_ids:
            conn.execute(
                "INSERT OR IGNORE INTO tasks (id, batch_id, status, current_version, stage,"
                " created_at, updated_at) VALUES (?, ?, ?, 1, ?, ?, ?)",
                (task_id, batch_id, TaskStatus.CREATED, Stage.QUEUED, now, now),
            )
            conn.execute(
                "INSERT OR IGNORE INTO jobs (id, task_id, version, type, input, status, due_at,"
                " priority, created_at) VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?)",
                (
                    f"{task_id}-v1-generate-r0",
                    task_id,
                    JobType.GENERATE,
                    json.dumps({"origin": "initial"}),
                    JobStatus.READY,
                    now,
                    JOB_PRIORITY[JobType.GENERATE],
                    now,
                ),
            )
    return {"batch_id": batch_id, "task_ids": task_ids, "created": existing == 0}


def _batch_rows(conn: sqlite3.Connection, batch_id: str) -> tuple[list[sqlite3.Row], dict]:
    """The batch's tasks (in submission order) and their jobs grouped by task id."""
    tasks = conn.execute(
        "SELECT * FROM tasks WHERE batch_id = ? ORDER BY id", (batch_id,)
    ).fetchall()
    if not tasks:
        raise NotFound(f"batch {batch_id!r} not found")
    jobs_by_task = defaultdict(list)
    for job in conn.execute(
        "SELECT j.* FROM jobs j JOIN tasks t ON t.id = j.task_id"
        " WHERE t.batch_id = ? ORDER BY j.created_at, j.id",
        (batch_id,),
    ):
        jobs_by_task[job["task_id"]].append(job)
    return tasks, jobs_by_task


def _scored_rollouts(jobs, version: int) -> dict[int, str]:
    """slot -> VALID_PASS / VALID_FAIL for one version."""
    return {
        j["slot"]: j["outcome"]
        for j in jobs
        if j["type"] == JobType.ROLLOUT and j["version"] == version and j["outcome"] in SCORED_OUTCOMES
    }


def pass_pattern(jobs, version: int, n_runs: int) -> str:
    scored = _scored_rollouts(jobs, version)
    marks = {Outcome.VALID_PASS: "✓", Outcome.VALID_FAIL: "✗"}
    return "".join(marks.get(scored.get(slot), "·") for slot in range(1, n_runs + 1))


def _sum_cost(jobs) -> float | None:
    """Sum of recorded costs; None if nothing has a recorded cost yet."""
    costs = [j["cost_usd"] for j in jobs if j["cost_usd"] is not None]
    return round(sum(costs), 6) if costs else None


def get_batch(conn: sqlite3.Connection, batch_id: str, settings: Settings) -> dict:
    tasks, jobs_by_task = _batch_rows(conn, batch_id)
    return {
        "batch_id": batch_id,
        "counts_by_status": dict(Counter(t["status"] for t in tasks)),
        "tasks": [
            {
                "id": t["id"],
                "status": t["status"],
                "reason": t["reason"],
                "current_version": t["current_version"],
                "stage": t["stage"],
                "pass_pattern": pass_pattern(jobs_by_task[t["id"]], t["current_version"], settings.n_runs),
                "cost_usd": _sum_cost(jobs_by_task[t["id"]]),
            }
            for t in tasks
        ],
    }


def _version_view(task: sqlite3.Row, version: int, jobs, settings: Settings) -> dict:
    vjobs = [j for j in jobs if j["version"] == version]
    generates = [j for j in vjobs if j["type"] == JobType.GENERATE]
    validates = [j for j in vjobs if j["type"] == JobType.VALIDATE]
    scored = _scored_rollouts(jobs, version)
    pass_count = sum(o == Outcome.VALID_PASS for o in scored.values())

    # The version's origin is its first non-repair generate job.
    first = next((j for j in generates if (_json(j["input"]) or {}).get("origin") != "repair"), None)
    first_input = _json(first["input"]) if first else {}
    built = [j for j in generates if j["outcome"] == Outcome.OK]

    if version == task["current_version"]:
        state = task["stage"]
    elif len(scored) == settings.n_runs:
        # Only an out-of-band (or ambiguous-task) version is ever superseded.
        in_band = settings.band_low <= pass_count <= settings.band_high
        state = Stage.BAND_OK if in_band else Stage.OUT_OF_BAND
    else:
        state = Stage.INVALID

    return {
        "n": version,
        "origin": first_input.get("origin"),
        "operator": first_input.get("operator"),
        "state": state,
        "bundle_sha256": built[-1]["bundle_sha256"] if built else None,
        "repairs": sum((_json(j["input"]) or {}).get("origin") == "repair" for j in generates),
        "gates": (_json(validates[-1]["result"]) or {}).get("gates") if validates else None,
        "pass_count": pass_count if len(scored) == settings.n_runs else None,
        "pass_pattern": pass_pattern(jobs, version, settings.n_runs),
        "cost_usd": _sum_cost(vjobs),
    }


def _decision(task: sqlite3.Row, jobs, settings: Settings) -> str:
    if task["status"] == TaskStatus.ACCEPTED:
        v = task["accepted_version"]
        passes = sum(o == Outcome.VALID_PASS for o in _scored_rollouts(jobs, v).values())
        return (
            f"ACCEPTED: v{v} passed {passes}/{settings.n_runs} valid rollouts "
            f"(band {settings.band_low}–{settings.band_high})"
        )
    if task["status"] == TaskStatus.REJECTED:
        return f"REJECTED ({task['reason']}) at v{task['current_version']}"
    return f"PENDING: v{task['current_version']} is {task['stage']}"


def get_task(conn: sqlite3.Connection, task_id: str, settings: Settings) -> dict:
    """The verdict for one task: every version, gate, rollout and the decision."""
    task = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if task is None:
        raise NotFound(f"task {task_id!r} not found")
    jobs = conn.execute(
        "SELECT * FROM jobs WHERE task_id = ? ORDER BY created_at, id", (task_id,)
    ).fetchall()
    rollouts = sorted(
        (j for j in jobs if j["type"] == JobType.ROLLOUT),
        key=lambda j: (j["version"], j["slot"] or 0, j["retry"]),
    )
    return {
        "task": {
            "id": task["id"],
            "batch_id": task["batch_id"],
            "name": task["name"],
            "slot": _json(task["slot"]),
            "status": task["status"],
            "reason": task["reason"],
            "current_version": task["current_version"],
            "stage": task["stage"],
            "accepted_version": task["accepted_version"],
            "created_at": task["created_at"],
            "updated_at": task["updated_at"],
        },
        "versions": [
            _version_view(task, v, jobs, settings) for v in range(1, task["current_version"] + 1)
        ],
        "rollouts": [
            {
                "id": j["id"],
                "version": j["version"],
                "slot": j["slot"],
                "retry": j["retry"],
                "status": j["status"],
                "outcome": j["outcome"],
                "reward": j["reward"],
                "error": j["error"],
                "trace_uri": j["trace_uri"],
                "trace_metrics": _json(j["trace_metrics"]),
                "failure": _json(j["failure"]),
                "cost_usd": j["cost_usd"],
            }
            for j in rollouts
        ],
        "decision": _decision(task, jobs, settings),
        "reason": task["reason"],
        "cost_usd": _sum_cost(jobs),
    }


def batch_report(conn: sqlite3.Connection, batch_id: str, settings: Settings) -> dict:
    """summary.json (docs/PRD.md §4). Counts are over initial candidates (tasks),
    not versions; `null` means not measured yet, never zero."""
    tasks, jobs_by_task = _batch_rows(conn, batch_id)
    all_jobs = [j for t in tasks for j in jobs_by_task[t["id"]]]

    def any_validate(task_id, check) -> bool:
        return any(
            check(j) for j in jobs_by_task[task_id] if j["type"] == JobType.VALIDATE and j["status"] == JobStatus.DONE
        )

    histogram = {str(n): 0 for n in range(settings.n_runs + 1)}
    for t in tasks:
        for v in range(1, t["current_version"] + 1):
            scored = _scored_rollouts(jobs_by_task[t["id"]], v)
            if len(scored) == settings.n_runs:
                histogram[str(sum(o == Outcome.VALID_PASS for o in scored.values()))] += 1

    rollouts = [j for j in all_jobs if j["type"] == JobType.ROLLOUT]
    authoring = _sum_cost(j for j in all_jobs if j["type"] != JobType.ROLLOUT)
    evaluation = _sum_cost(rollouts)
    total = None if authoring is None and evaluation is None else round((authoring or 0) + (evaluation or 0), 6)
    accepted = [t for t in tasks if t["status"] == TaskStatus.ACCEPTED]

    finished = [j["finished_at"] for j in all_jobs if j["finished_at"]]
    wall_clock = (
        round((parse_ts(max(finished)) - parse_ts(min(t["created_at"] for t in tasks))).total_seconds())
        if finished
        else None
    )
    # More than one config hash means rollouts weren't all measured the same way.
    hashes = sorted({j["config_hash"] for j in rollouts if j["config_hash"]})

    return {
        "batch_id": batch_id,
        "candidates": len(tasks),
        "structural_pass": sum(
            any_validate(t["id"], lambda j: ((_json(j["result"]) or {}).get("gates") or {}).get("structural") is True)
            for t in tasks
        ),
        "functional_pass": sum(any_validate(t["id"], lambda j: j["outcome"] == Outcome.OK) for t in tasks),
        "accepted_v1": sum(t["accepted_version"] == 1 for t in accepted),
        "accepted_total": len(accepted),
        "rejected": dict(Counter(t["reason"] for t in tasks if t["status"] == TaskStatus.REJECTED)),
        "in_progress": sum(t["status"] in (TaskStatus.CREATED, TaskStatus.CALIBRATING) for t in tasks),
        "pass_count_histogram": histogram,
        "valid_rollouts": sum(j["outcome"] in SCORED_OUTCOMES for j in rollouts),
        "invalid_rollouts": sum(j["outcome"] == Outcome.INVALID for j in rollouts),
        "cost_usd": {
            "authoring": authoring,
            "evaluation": evaluation,
            "total": total,
            "per_accepted": round(total / len(accepted), 6) if accepted and total is not None else None,
        },
        "wall_clock_s": wall_clock,
        "config_hash": hashes[0] if len(hashes) == 1 else (hashes or None),
    }
