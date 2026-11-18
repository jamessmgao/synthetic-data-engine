"""
Read models behind the API (docs/PRD.md §4): the task list, the task verdict,
batches, and the report (with its yield block) over a set of tasks, computed from
the `pipeline` store. Creation goes through `api.submit`; nothing here writes.

What the services write and these read (PRD §5.1):
  - create / revise (Generation Service): `input.origin` in {initial, repair,
    revision}, optional `input.operator`; on success `outcome=OK` and `bundle_sha256`.
  - validate: `result.gates` = {name: {"passed": bool, ...}}; `outcome=OK`
    only if every gate passed.
  - rollout: `slot` 1..N, `outcome` VALID_PASS | VALID_FAIL | INVALID; the Trace
    Parser adds `trace_metrics` and `failure`.
  - analyze (Trace Parser): `result.action` and its review cost.
"""

from collections import Counter, defaultdict
from datetime import datetime
from statistics import median
from typing import Any

from evaluation.calibration import eval_config, in_band
from evaluation.verdict import decision_text, pass_pattern, version_input
from shared.storage import budget
from shared.jobs import tasks as task_state
from shared.storage.sql import SqlStore, row_dict
from shared.contracts.models import (
    GENERATION_TYPES,
    SCORED_OUTCOMES,
    JobStatus,
    JobType,
    Outcome,
    Stage,
    TaskStatus,
)


class NotFound(Exception):
    pass


# Evaluation spend: the runs and the Trace Parser's reviews. Everything else (world,
# world_check, create/revise, the gates) is authoring.
_EVALUATED = (JobType.ROLLOUT, JobType.ANALYZE)


def _jobs_by_task(store: SqlStore, tasks: list[dict]) -> dict[str, list[dict]]:
    """The tasks' jobs, grouped by task id, in creation order."""
    jobs_by_task: dict[str, list[dict]] = defaultdict(list)
    ids = [t["id"] for t in tasks]
    for start in range(0, len(ids), 500):  # keep the IN list a sane size
        chunk = ids[start:start + 500]
        rows = store.conn.execute(
            f"SELECT * FROM jobs WHERE task_id IN ({','.join('?' * len(chunk))}) ORDER BY created_at, id",
            chunk,
        ).fetchall()
        for job in map(row_dict, rows):
            jobs_by_task[job["task_id"]].append(job)
    return jobs_by_task


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


def list_tasks(store: SqlStore, status: str | None = None, since: str | None = None,
               batch_id: str | None = None) -> list[dict]:
    """Every task (optionally one status, one batch, or created at/after `since`), oldest first."""
    tasks = store.tasks(status=status, since=since, batch_id=batch_id)
    jobs_by_task = _jobs_by_task(store, tasks)
    n_runs = eval_config()["n_runs"]
    return [
        {
            "id": t["id"],
            "name": t["name"],
            "status": t["status"],
            "reason": t["reason"],
            "current_version": t["current_version"],
            "stage": t["stage"],
            "pass_pattern": pass_pattern(jobs_by_task[t["id"]], t["current_version"], n_runs),
            "cost_usd": _sum_cost(jobs_by_task[t["id"]]),
            "created_at": t["created_at"],
        }
        for t in tasks
    ]


def _version_view(task: dict, version: int, jobs: list[dict], n_runs: int) -> dict:
    vjobs = [j for j in jobs if j["version"] == version]
    generates = [j for j in vjobs if j["type"] in GENERATION_TYPES]
    validates = [j for j in vjobs if j["type"] == JobType.VALIDATE]
    scored = _scored(jobs, version)
    pass_count = sum(o == Outcome.VALID_PASS for o in scored.values())

    # The version's origin is its first non-repair world / create / revise job (a
    # mutated agent-mode version starts with a world job).
    first_input = version_input(vjobs)
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
        "mutation_operators": (first_input.get("mutation_operators")
                               or (first_input.get("mutation") or {}).get("operators") or []),
        "state": state,
        "world": _world_view(vjobs),
        "bundle_sha256": built[-1]["bundle_sha256"] if built else None,
        "repairs": sum((j["input"] or {}).get("origin") == "repair" for j in generates),
        "gates": (validates[-1]["result"] or {}).get("gates") if validates else None,
        "pass_count": pass_count if len(scored) == n_runs else None,
        "pass_pattern": pass_pattern(jobs, version, n_runs),
        "cost_usd": _sum_cost(vjobs),
        "analysis": _analysis_view(vjobs, scored),
    }


def _analysis_view(vjobs: list[dict], scored: dict[int, str]) -> dict | None:
    """The Trace Parser's decision on the version (its last analyze job that decided):
    the action, the pass review (why it was easy, the cues, the suggestions), the
    mutation chosen for the next version with the rules that fired, and the passing
    runs' signals those rules read (calibration.choose_mutations). None until decided."""
    decided = [j for j in vjobs if j["type"] == JobType.ANALYZE and j["status"] == JobStatus.DONE
               and (j["result"] or {}).get("action") not in (None, "wait", "stale")]
    if not decided:
        return None
    result = decided[-1]["result"]
    review = result.get("easy_analysis")
    passing = [j for j in vjobs if j["type"] == JobType.ROLLOUT and j["outcome"] == Outcome.VALID_PASS
               and scored.get(j["slot"]) == Outcome.VALID_PASS]
    metrics = [j["trace_metrics"] or {} for j in passing]
    files = [m["files_modified"] for m in metrics if m.get("files_modified") is not None]
    steps = [m["llm_calls"] for m in metrics if m.get("llm_calls")]
    return {
        "job_id": decided[-1]["id"],
        "action": result.get("action"),
        "summary": result.get("summary"),
        "direction": result.get("direction"),
        "operator": result.get("operator"),
        "mutation": result.get("mutation"),
        "review": {k: review.get(k) for k in (
            "trusted", "why_easy", "simple_strategy", "fix_applied", "cues", "suggestions", "runs_reviewed",
            "review_error",
        ) if k in review} if review else None,
        "signals": {
            "passing_runs": len(passing),
            "files_modified_median": median(files) if files else None,
            "steps_median": median(steps) if steps else None,
            "cues": len((review or {}).get("cues") or []),
        },
    }


def _world_view(vjobs: list[dict]) -> dict | None:
    """The version's world (agent mode, plan §3.3): the world job's sha and lineage, or
    the world a create/revise job reports it was built on. None in one-shot mode."""
    worlds = [j for j in vjobs if j["type"] == JobType.WORLD]
    built_on = [j for j in vjobs if j["type"] in GENERATION_TYPES and (j["result"] or {}).get("world_sha256")]
    if not worlds and not built_on:
        return None
    done = [j for j in worlds if j["outcome"] == Outcome.OK]
    result = (done[-1]["result"] if done else None) or {}
    sha = result.get("world_sha256") or ((built_on[-1]["result"] or {}).get("world_sha256") if built_on else None)
    return {
        "world_sha256": sha,
        "lineage": result.get("lineage"),
        "status": worlds[-1]["status"] if worlds else None,
        "outcome": worlds[-1]["outcome"] if worlds else None,
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
            "id", "name", "slot", "status", "reason", "current_version", "stage",
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


def report(store: SqlStore, task_ids: list[str] | None = None, since: str | None = None) -> dict:
    """summary.json (docs/PRD.md §4) over the named tasks, or those created at/after
    `since` (all tasks if neither is given). Counts are over initial candidates
    (tasks), not versions; `null` means not measured yet, never zero."""
    tasks = store.tasks(ids=task_ids, since=since)
    if task_ids and len(tasks) != len(set(task_ids)):
        missing = sorted(set(task_ids) - {t["id"] for t in tasks})
        raise NotFound(f"unknown task ids: {missing}")
    if not tasks:
        raise NotFound("no tasks match")
    return {**_summary(store, tasks), "budget": budget.status(store.conn)}


def _summary(store: SqlStore, tasks: list[dict]) -> dict:
    jobs_by_task = _jobs_by_task(store, tasks)
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
    # Authoring: drafting and the gates. Evaluation: the runs and the Trace Parser's reviews.
    authoring = _sum_cost(j for j in all_jobs if j["type"] not in _EVALUATED)
    evaluation = _sum_cost(j for j in all_jobs if j["type"] in _EVALUATED)
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

    summary = {
        "task_ids": [t["id"] for t in tasks],
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
        # Stopped by a user: neither yield nor failure (not in `rejected`, not in learnable_rate).
        "cancelled": sum(t["status"] == TaskStatus.CANCELLED for t in tasks),
        "pass_count_histogram": histogram,
        "valid_rollouts": sum(j["outcome"] in SCORED_OUTCOMES for j in rollouts),
        "invalid_rollouts": sum(j["outcome"] == Outcome.INVALID or j["status"] == JobStatus.DEAD for j in rollouts),
        "cost_usd": {
            # Authoring: world, world_check, create/revise and the gates (agent mode's
            # authoring trials dominate spend). Evaluation: the runs and their reviews.
            "authoring": authoring,
            "evaluation": evaluation,
            "total": total,
            "authoring_per_task": round(authoring / len(tasks), 6) if authoring is not None else None,
            "evaluation_per_task": round(evaluation / len(tasks), 6) if evaluation is not None else None,
            "per_accepted": round(total / len(accepted), 6) if accepted and total is not None else None,
        },
        "wall_clock_s": wall_clock,
        "config_hash": hashes[0] if len(hashes) == 1 else (hashes or None),
        "instruction_tokens": _instruction_tokens(all_jobs),
        "by_family": _breakdown(tasks, jobs_by_task, "family"),
        "by_mechanism": _breakdown(tasks, jobs_by_task, "mechanism"),
    }
    return {**summary, "yield": _yield(summary)}


def _instruction_tokens(jobs) -> dict[str, dict]:
    """The rendered instruction's size per authoring step (result.authoring.instruction_size,
    an estimate): the context every agent step of that trial starts from. {step: {n, mean, max}}."""
    sizes: dict[str, list[int]] = {}
    for j in jobs:
        tokens = (((j["result"] or {}).get("authoring") or {}).get("instruction_size") or {}).get("tokens_est")
        if tokens is None:
            continue
        step = j["type"]
        if step == JobType.WORLD:
            origin = (j["input"] or {}).get("origin") or "initial"
            step = {"mutation": "world_mutation", "repair": "world_repair"}.get(origin, "world")
        sizes.setdefault(str(step), []).append(tokens)
    return {step: {"n": len(v), "mean": round(sum(v) / len(v)), "max": max(v)} for step, v in sorted(sizes.items())}


def _breakdown(tasks: list[dict], jobs_by_task: dict[str, list[dict]], key: str) -> dict[str, dict]:
    """Counts and pass rates per value of a spec v2 field (family or mechanism). Tasks
    without one (v1 specs, raw prompts) are left out. `pass_rate` is passes over valid
    rollouts across every version; `learnable_rate` is accepted over finished tasks."""
    groups: dict[str, list[dict]] = defaultdict(list)
    for t in tasks:
        value = (t.get("slot") or {}).get(key)
        if value:
            groups[value].append(t)
    out = {}
    for value, group in sorted(groups.items()):
        rollouts = [j for t in group for j in jobs_by_task[t["id"]]
                    if j["type"] == JobType.ROLLOUT and j["outcome"] in SCORED_OUTCOMES]
        passes = sum(j["outcome"] == Outcome.VALID_PASS for j in rollouts)
        finished = [t for t in group if t["status"] in (TaskStatus.ACCEPTED, TaskStatus.REJECTED)]
        accepted = sum(t["status"] == TaskStatus.ACCEPTED for t in group)
        group_jobs = [j for t in group for j in jobs_by_task[t["id"]]]
        authoring = _sum_cost(j for j in group_jobs if j["type"] not in _EVALUATED)
        evaluation = _sum_cost(j for j in group_jobs if j["type"] in _EVALUATED)
        out[value] = {
            "candidates": len(group),
            "finished": len(finished),
            "accepted": accepted,
            "rejected": len(finished) - accepted,
            "learnable_rate": round(accepted / len(finished), 4) if finished else None,
            "valid_rollouts": len(rollouts),
            "pass_rate": round(passes / len(rollouts), 4) if rollouts else None,
            "cost_usd": {"authoring": authoring, "evaluation": evaluation,
                         "authoring_per_task": round(authoring / len(group), 6) if authoring is not None else None},
        }
    return out


def _yield(summary: dict) -> dict:
    """The headline number: the share of finished candidates accepted as learnable.
    Tasks still in progress, and tasks a user cancelled, are left out of the denominator;
    null until one finishes."""
    finished = summary["candidates"] - summary["in_progress"] - summary["cancelled"]
    return {
        "candidates": summary["candidates"],
        "functional": summary["functional_pass"],
        "accepted": summary["accepted_total"],
        "accepted_v1": summary["accepted_v1"],
        "learnable_rate": round(summary["accepted_total"] / finished, 4) if finished else None,
    }


def cancel_tasks(store: SqlStore, task_ids: list[str], by: str = "user", note: str = "") -> dict:
    """Cancel tasks in one transaction (pipeline/tasks.cancel). Idempotent: an already
    decided task is left as it is and counted under `already_decided`."""
    with store.tx() as conn:
        results = [task_state.cancel(conn, t, by, note) for t in task_ids]
    return {"cancelled": sum(r["cancelled"] for r in results),
            "already_decided": sum(not r["cancelled"] for r in results),
            "closed_jobs": sum(r["closed_jobs"] for r in results),
            "tasks": {r["task_id"]: r["status"] for r in results}}


def cancel_task(store: SqlStore, task_id: str, by: str = "user", note: str = "") -> dict:
    if store.task(task_id) is None:
        raise NotFound(f"task {task_id!r} not found")
    return {"task_id": task_id, **cancel_tasks(store, [task_id], by, note)}


def cancel_batch(store: SqlStore, batch_id: str, by: str = "user", note: str = "") -> dict:
    return {"batch_id": batch_id, **cancel_tasks(store, [t["id"] for t in _batch_tasks(store, batch_id)], by, note)}


def list_batches(store: SqlStore) -> list[dict]:
    """Every batch with its outcome counts, newest first."""
    return store.batches()


def _batch_tasks(store: SqlStore, batch_id: str) -> list[dict]:
    tasks = store.tasks(batch_id=batch_id)
    if not tasks:
        raise NotFound(f"batch {batch_id!r} not found")
    return tasks


def get_batch(store: SqlStore, batch_id: str) -> dict:
    """One batch: task count per status and each task's summary."""
    tasks = _batch_tasks(store, batch_id)
    return {
        "batch_id": batch_id,
        "counts_by_status": dict(Counter(t["status"] for t in tasks)),
        "tasks": list_tasks(store, batch_id=batch_id),
    }


def batch_report(store: SqlStore, batch_id: str) -> dict:
    """summary.json over one batch's tasks."""
    return {"batch_id": batch_id, **_summary(store, _batch_tasks(store, batch_id)),
            "budget": budget.status(store.conn, batch_id), "pipeline_budget": budget.status(store.conn)}
