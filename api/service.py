"""
Read models behind the API (docs/PRD.md §4): the task list, the task verdict,
batches, and the report (with its yield block) over a set of tasks, computed from
the `pipeline` store. Creation goes through `api.submit`; nothing here writes.

What the services write and these read (PRD §5.1):
  - create / revise (Generation Service): `input.origin` in {initial, repair,
    revision}, optional `input.operator`; on success `outcome=OK` and `bundle_sha256`.
  - validate: `result.gates` = {name: {"passed": bool, ...}}; `outcome=OK`
    only if every gate passed.
  - rollout: `slot` 1..N, `outcome` VALID_PASS | VALID_FAIL | INVALID | TIMEOUT (until ruled) | SKIPPED; the Trace
    Parser adds `trace_metrics` and `failure`.
  - analyze (Trace Parser): `result.action` and its review cost.
"""

import json
import os
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from statistics import median
from typing import Any

from evaluation.calibration import eval_config, in_band
from evaluation.verdict import decision_text, pass_pattern, version_input
from api import submit
from shared.storage import budget
from shared.jobs import tasks as task_state
from shared.storage.artifacts import LocalArtifactStore
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


def _settled(jobs: list[dict], version: int, scored: dict[int, str], n_runs: int) -> bool:
    """Every slot of the version is scored, or SKIPPED by early stopping once the verdict was certain."""
    skipped = {j["slot"] for j in jobs if j["type"] == JobType.ROLLOUT and j["version"] == version
               and j["outcome"] == Outcome.SKIPPED and j["slot"] not in scored}
    return len(scored) + len(skipped) == n_runs


def _sum_cost(jobs) -> float | None:
    """Sum of recorded costs; None if nothing has a recorded cost yet."""
    costs = [j["cost_usd"] for j in jobs if j["cost_usd"] is not None]
    return round(sum(costs), 6) if costs else None


def dev_batches() -> frozenset[str]:
    """Batches counted as dev whatever they cost (SDE_DEV_BATCHES, a comma list; aws-prod
    sets it): early or abandoned real batches the reports shouldn't mix in."""
    return frozenset(b.strip() for b in os.environ.get("SDE_DEV_BATCHES", "").split(",") if b.strip())


def _is_dev(task: dict, jobs) -> bool:
    """A dev task: its batch is in dev_batches(), or its recorded cost is exactly 0 (mock
    models: the lt-* load tests, prodmock, other mock runs). One with nothing recorded yet
    (None) isn't: a real task just created."""
    return task.get("batch_id") in dev_batches() or _sum_cost(jobs) == 0


def _gate_passed(gate: Any) -> bool:
    return gate is True or (isinstance(gate, dict) and gate.get("passed") is True)


def list_tasks(store: SqlStore, status: str | None = None, since: str | None = None,
               batch_id: str | None = None, scope: str = "all") -> list[dict]:
    """Every task (optionally one status, one batch, created at/after `since`, or one scope:
    prod / dev; all by default, as before), oldest first."""
    tasks = store.tasks(status=status, since=since, batch_id=batch_id)
    jobs_by_task = _jobs_by_task(store, tasks)
    tasks = [t for t in tasks if _in_scope(scope, t, jobs_by_task[t["id"]])]
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
            "dev": _is_dev(t, jobs_by_task[t["id"]]),
            "created_at": t["created_at"],
            **_taxonomy_summary(t, jobs_by_task[t["id"]], n_runs),
        }
        for t in tasks
    ]


def _taxonomy_summary(task: dict, jobs: list[dict], n_runs: int) -> dict:
    """The list view's taxonomy and revision history (the UI's analytics): the spec's family,
    mechanism and environment, the batch, the accepted version, whether the task reused a
    world, and per version its origin and pass count (as _version_view has them)."""
    slot = task.get("slot") or {}
    if isinstance(slot, str):
        slot = json.loads(slot or "{}")
    versions = []
    for n in range(1, task["current_version"] + 1):
        vjobs = [j for j in jobs if j["version"] == n]
        scored = _scored(jobs, n)
        versions.append({"n": n, "origin": version_input(vjobs).get("origin"),
                         "pass_count": (sum(o == Outcome.VALID_PASS for o in scored.values())
                                        if _settled(jobs, n, scored, n_runs) else None)})
    return {"family": slot.get("family"), "mechanism": slot.get("mechanism"),
            "environment": slot.get("environment"), "batch_id": task.get("batch_id"),
            "accepted_version": task.get("accepted_version"), "reused_world": "reused_world" in slot,
            "versions": versions}


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
    elif _settled(jobs, version, scored, n_runs):
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
        "pass_count": pass_count if _settled(jobs, version, scored, n_runs) else None,
        "pass_pattern": pass_pattern(jobs, version, n_runs),
        "cost_usd": _sum_cost(vjobs),
        "analysis": _analysis_view(vjobs, scored),
        **_leak_reviews(generates, validates, built[-1]["bundle_sha256"] if built else None),
    }


# The leak gate's detail (validator/gates.py gate_leak), as the task view shows it.
_LEAK_KEYS = ("passed", "verdict", "leaks", "unverified_quotes", "direct", "strong_hints", "blind",
              "blind_leak", "cut_fields", "inconclusive", "skipped", "cost_usd", "adopted")


def _leak_view(job: dict, detail: dict, sha: str | None = None) -> dict:
    sha = (job["result"] or {}).get("bundle_sha256") or job["bundle_sha256"] or sha
    return {"job_id": job["id"], "job_type": job["type"], "origin": (job["input"] or {}).get("origin"),
            "outcome": job["outcome"], "bundle_sha256": sha,
            **{k: detail.get(k) for k in _LEAK_KEYS},
            # The raw record: /v1/artifacts/leak_reviews/<sha>/file?path=review.json
            "raw": {"kind": "leak_reviews", "key": sha, "path": "review.json"} if sha else None}


def _leak_reviews(generates: list[dict], validates: list[dict], final_sha: str | None) -> dict:
    """A version's leak review: the gate on its final bundle (the last validate's, else the
    create / revise job's own), and every earlier attempt whose draft was another bundle
    (a create or repair the review sent back). The gate records it whether it passed or not."""
    reviewed = [(j, (j["result"] or {}).get("leak")) for j in generates]
    reviewed = [(j, d) for j, d in reviewed if isinstance(d, dict)]
    final = next(((j, ((j["result"] or {}).get("gates") or {}).get("leak")) for j in reversed(validates)
                  if isinstance(((j["result"] or {}).get("gates") or {}).get("leak"), dict)), None)
    if final is None:
        final = next(((j, d) for j, d in reversed(reviewed)
                      if final_sha and (j["result"] or {}).get("bundle_sha256") == final_sha), None)
    shown = final[0]["id"] if final else None
    return {
        "leak_review": _leak_view(*final, sha=final_sha) if final else None,
        "leak_attempts": [_leak_view(j, d) for j, d in reviewed
                          if j["id"] != shown and (j["result"] or {}).get("bundle_sha256") != final_sha],
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
    # files_modified is the list of paths a run edited (trace_parser.files_modified); count them
    # as calibration.choose_mutations does.
    files = [len(m["files_modified"]) for m in metrics if m.get("files_modified") is not None]
    steps = [m["llm_calls"] for m in metrics if m.get("llm_calls")]
    return {
        "job_id": decided[-1]["id"],
        "action": result.get("action"),
        "summary": result.get("summary"),
        # Decided before all runs were scored (its result was already certain): {why, passes,
        # remaining, scored, of}; None when every run was scored.
        "early_stopped": result.get("early_stopped"),
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
    the world a create/revise job reports it was built on, or (a reused world, while its
    create still runs) the one its input names. None in one-shot mode. `reused` is set
    when the version's create started from a pooled world (POST /v1/batches reuse_worlds)."""
    worlds = [j for j in vjobs if j["type"] == JobType.WORLD]
    built_on = [j for j in vjobs if j["type"] in GENERATION_TYPES and (j["result"] or {}).get("world_sha256")]
    given = [j for j in vjobs if j["type"] in GENERATION_TYPES and (j["input"] or {}).get("world_sha256")]
    if not worlds and not built_on and not given:
        return None
    done = [j for j in worlds if j["outcome"] == Outcome.OK]
    result = (done[-1]["result"] if done else None) or {}
    sha = (result.get("world_sha256")
           or ((built_on[-1]["result"] or {}).get("world_sha256") if built_on else None)
           or ((given[-1]["input"] or {}).get("world_sha256") if given else None))
    return {
        "world_sha256": sha,
        "lineage": result.get("lineage"),
        "status": worlds[-1]["status"] if worlds else None,
        "outcome": worlds[-1]["outcome"] if worlds else None,
        **({"reused": True} if any((j["input"] or {}).get("reused") for j in given) else {}),
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
    slot = task.get("slot") or {}
    return {
        "task": {k: task[k] for k in (
            "id", "name", "slot", "status", "reason", "current_version", "stage",
            "accepted_version", "batch_id", "created_at", "updated_at",
        )},
        # A task started on a pooled world (reuse_worlds): which world, the task that built it, and
        # why the selection policy picked it, to check novelty against the world's earlier tasks.
        "reuse": ({"world_sha256": slot.get("reused_world"), "from_task": slot.get("reused_from"),
                   "tier": slot.get("reuse_tier"), "reason": slot.get("reuse_reason")}
                  if isinstance(slot, dict) and slot.get("reused_world") else None),
        "versions": [_version_view(task, v, jobs, n_runs) for v in range(1, task["current_version"] + 1)],
        "rollouts": [
            {k: j[k] for k in (
                "id", "version", "slot", "retry", "status", "outcome", "reward", "error",
                "trace_uri", "trace_metrics", "failure", "cost_usd",
            )}
            for j in rollouts
        ],
        "jobs": [_job_view(j) for j in jobs],
        "decision": decision_text(task, jobs),
        "reason": task["reason"],
        "cost_usd": _sum_cost(jobs),
        "dev": _is_dev(task, jobs),
    }


def _job_view(job: dict) -> dict:
    """One job row without its large blobs: what ran, where, for how long, and how it ended."""
    return {
        **{k: job[k] for k in (
            "id", "task_id", "type", "version", "slot", "retry", "status", "outcome", "attempts",
            "lease_owner", "bundle_sha256", "error", "trace_uri", "cost_usd",
            "created_at", "started_at", "finished_at",
        )},
        "origin": (job["input"] or {}).get("origin"),
    }


def queue(store: SqlStore) -> dict:
    """Job counts by type and status, and every job that is running now (LEASED), oldest first."""
    counts: dict[str, dict[str, int]] = defaultdict(dict)
    for r in store.conn.execute("SELECT type, status, COUNT(*) AS n FROM jobs GROUP BY type, status").fetchall():
        counts[r["type"]][r["status"]] = int(r["n"])
    running = store.conn.execute(
        "SELECT * FROM jobs WHERE status = ? ORDER BY started_at, id", (JobStatus.LEASED,)
    ).fetchall()
    return {"counts": counts, "running": [_job_view(row_dict(r)) for r in running]}


# --- artifacts: the files behind a result (read-only) -------------------------

ARTIFACT_KINDS = ("bundles", "worlds", "traces", "leak_reviews")
_ARTIFACT_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,200}$")
MAX_LISTED_FILES = 5000
MAX_FILE_BYTES = 1_000_000


def _artifact_root(artifacts: LocalArtifactStore, kind: str, key: str) -> Path:
    """The local dir of a bundle or world (by sha) or a job's trace (by job id),
    fetched from S3 first when the store has one."""
    if kind not in ARTIFACT_KINDS or not _ARTIFACT_KEY.match(key):
        raise NotFound(f"no artifact {kind}/{key}")
    if kind == "traces":
        root = artifacts.trace_dir(key) if artifacts.fetch_trace(key) else None
    elif kind == "leak_reviews":  # by bundle sha: leak_reviews/<sha>/review.json (shared/jobs/worker.py)
        root = artifacts.root / kind / key if artifacts.fetch_file(f"{kind}/{key}/review.json") else None
    else:
        root = artifacts.tree_path(kind, key)
    if root is None or not root.is_dir():
        raise NotFound(f"no artifact {kind}/{key}")
    return root


def artifact_files(artifacts: LocalArtifactStore, kind: str, key: str) -> dict:
    """Every file of one artifact: relative path and size, sorted (at most MAX_LISTED_FILES)."""
    root = _artifact_root(artifacts, kind, key)
    files = sorted(p for p in root.rglob("*") if p.is_file() and p.name != ".complete")
    return {
        "kind": kind, "key": key, "total": len(files),
        "files": [{"path": p.relative_to(root).as_posix(), "size": p.stat().st_size}
                  for p in files[:MAX_LISTED_FILES]],
    }


def artifact_file(artifacts: LocalArtifactStore, kind: str, key: str, path: str) -> dict:
    """One file's text (its first MAX_FILE_BYTES); binary files report only their size."""
    root = _artifact_root(artifacts, kind, key).resolve()
    target = (root / path).resolve()
    if not target.is_relative_to(root) or not target.is_file():
        raise NotFound(f"no file {path!r} in {kind}/{key}")
    size = target.stat().st_size
    with target.open("rb") as f:
        head = f.read(MAX_FILE_BYTES)
    binary = b"\0" in head[:8192]
    return {"path": path, "size": size, "binary": binary, "truncated": size > MAX_FILE_BYTES,
            "text": None if binary else head.decode("utf-8", errors="replace")}


def report(store: SqlStore, task_ids: list[str] | None = None, since: str | None = None,
           scope: str = "prod") -> dict:
    """summary.json (docs/PRD.md §4) over the named tasks, or those created at/after
    `since` (all tasks if neither is given). Counts are over initial candidates
    (tasks), not versions; `null` means not measured yet, never zero. Dev tasks (_is_dev)
    count only in `scope` dev or all (scope_of); `other_scope` says how many the other view has."""
    tasks = store.tasks(ids=task_ids, since=since)
    if task_ids and len(tasks) != len(set(task_ids)):
        missing = sorted(set(task_ids) - {t["id"] for t in tasks})
        raise NotFound(f"unknown task ids: {missing}")
    if not tasks:
        raise NotFound("no tasks match")
    return {**_summary(store, tasks, scope), "budget": budget.status(store.conn)}


SCOPES = ("prod", "dev", "all")


def scope_of(scope: str | None, include_dev: bool = False) -> str:
    """prod (the default: tasks with real spend), dev (mock runs only), or all. The older
    include_dev=true still means all."""
    scope = scope or ("all" if include_dev else "prod")
    if scope not in SCOPES:
        raise ValueError(f"scope must be one of {', '.join(SCOPES)}")
    return scope


def _in_scope(scope: str, task: dict, jobs: list[dict]) -> bool:
    return scope == "all" or (scope == "dev") == _is_dev(task, jobs)


def _summary(store: SqlStore, tasks: list[dict], scope: str = "prod") -> dict:
    jobs_by_task = _jobs_by_task(store, tasks)
    kept = [t for t in tasks if _in_scope(scope, t, jobs_by_task[t["id"]])]
    return {**_measure(tasks=kept, jobs_by_task=jobs_by_task), "scope": scope,
            # Tasks the other view (prod <-> dev) has; 0 for all.
            "other_scope": len(tasks) - len(kept),
            "include_dev": scope != "prod", "dev_excluded": len(tasks) - len(kept) if scope == "prod" else 0}


def _measure(tasks: list[dict], jobs_by_task: dict[str, list[dict]]) -> dict:
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
    # (No tasks left once dev ones are excluded, e.g. a load-test batch: an empty report.)
    wall_clock = (
        round((datetime.fromisoformat(max(finished))
               - datetime.fromisoformat(min(t["created_at"] for t in tasks))).total_seconds())
        if finished and tasks
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


def list_worlds(store: SqlStore, area: str | None = None) -> list[dict]:
    """The reusable world pool (api/submit.py world_pool), least-reused first."""
    pool = submit.world_pool(store.conn)
    if area:
        pool = [w for w in pool if w["family"] == area]
    return sorted(({k: w[k] for k in ("world_sha256", "family", "mechanism", "source_task", "source_status",
                                      "source_reason", "reuses")} for w in pool),
                  key=lambda w: (w["reuses"], w["family"], w["world_sha256"]))


def list_batches(store: SqlStore) -> list[dict]:
    """Every batch with its outcome counts, newest first; `dev` when it's in dev_batches()
    or everything it recorded cost exactly 0 (a mock run)."""
    spend = {r["batch_id"]: (r["n"], r["spent"]) for r in store.conn.execute(
        "SELECT t.batch_id AS batch_id, COUNT(j.cost_usd) AS n, COALESCE(SUM(j.cost_usd), 0) AS spent"
        " FROM tasks t JOIN jobs j ON j.task_id = t.id WHERE t.batch_id IS NOT NULL GROUP BY t.batch_id"
    ).fetchall()}
    listed = dev_batches()
    return [{**b, "dev": b["batch_id"] in listed
             or (spend.get(b["batch_id"], (0, 0))[0] > 0 and spend[b["batch_id"]][1] == 0)}
            for b in store.batches()]


def _batch_tasks(store: SqlStore, batch_id: str) -> list[dict]:
    tasks = store.tasks(batch_id=batch_id)
    if not tasks:
        raise NotFound(f"batch {batch_id!r} not found")
    return tasks


def get_batch(store: SqlStore, batch_id: str, scope: str = "all") -> dict:
    """One batch: task count per status and each task's summary (the summaries in `scope`)."""
    tasks = _batch_tasks(store, batch_id)
    return {
        "batch_id": batch_id,
        "counts_by_status": dict(Counter(t["status"] for t in tasks)),
        "tasks": list_tasks(store, batch_id=batch_id, scope=scope),
    }


def batch_report(store: SqlStore, batch_id: str, scope: str = "prod") -> dict:
    """summary.json over one batch's tasks in `scope` (prod by default)."""
    return {"batch_id": batch_id, **_summary(store, _batch_tasks(store, batch_id), scope),
            "budget": budget.status(store.conn, batch_id), "pipeline_budget": budget.status(store.conn)}
