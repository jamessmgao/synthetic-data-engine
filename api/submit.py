"""
Task creation: the API service's only write (PRD §4, §5.2 step 1).

One transaction creates the task (CREATED, v1 QUEUED) and its first job, whose
input carries the task's spec. A batch (`create_batch`) is N such tasks in one
transaction under a shared `batch_id` label, so their yield can be reported together;
nothing downstream knows about batches.

The job row is the outbox: it reaches its queue after the commit. From there the
Generation Service takes over; nothing here waits on it.

The spec comes from one of these requests:

- a text **prompt**: the idea the task is built around. It becomes the spec's
  `seed` (quality rules and difficulty targets still apply);
- an explicit **spec**: v1 `taxonomy` and/or `difficulty` fields (missing difficulty
  filled with the default profile), or v2 fields naming a `family` (missing ones
  sampled within that family);
- **nothing**: a slot is sampled, least-used across every task so far;
- a **raw prompt**: the complete user message, sent to GLM-5.1 verbatim instead of
  the template (for prompt experiments). The system prompt and the response schema
  still apply, so every rule the gates check is unchanged. The task's slot records
  it as `{"raw_prompt": ...}`: it bypasses the sampler and its diversity spread.

Spec v2 (docs/AGENTIC_AUTHORING_PLAN.md §3.1). A request may narrow sampling to an
`area` (a family, or a list of them) and filters (`mechanism`, `environment`,
`dial`). Any of those, a spec naming a `family`, or SDE_AUTHOR_MODE=agent makes the
sampler emit spec v2 (`shared.taxonomy.taxonomy.sample_spec_v2`), which also carries the v1
{taxonomy, difficulty} view one-shot authoring reads. A plain request in one-shot
mode samples v1 exactly as before.

The first job: `world` in agent mode for a v2 spec (the world author builds the
task's world from it, plan §3.3); `create` otherwise.

An optional client `task_id` makes a retry safe: the same id with the same
request inserts nothing; the same id with a different request is a conflict.
"""

import json
import secrets
from datetime import UTC, datetime
from typing import Any

from shared.contracts import models
from shared.taxonomy import world_selection
from shared.storage import Backend
from shared.storage.sql import insert_job, job_id, utcnow
from shared.jobs.jobqueue import JobQueue
from shared.contracts.models import JobType, Stage, TaskStatus
from shared.taxonomy.taxonomy import (
    DEFAULT_DIFFICULTY,
    FAMILIES,
    SHAPES,
    legacy_view,
    resolve_area,
    sample_spec,
    sample_spec_v2,
)

MAX_BATCH = 50
V2_FIELDS = ("family", "mechanism", "shape", "environment", "verification", "dial")


class TaskConflict(Exception):
    """The task id exists with a different request."""


class BatchConflict(Exception):
    """The batch id exists with a different request."""


def new_task_id() -> str:
    return f"t-{datetime.now(UTC):%Y%m%d}-{secrets.token_hex(4)}"


def new_batch_id() -> str:
    return f"b-{datetime.now(UTC):%Y%m%d}-{secrets.token_hex(4)}"


# --- what a request asks for --------------------------------------------------------


def filters_for(area: Any = None, mechanism: str | None = None, environment: str | None = None,
                dial: int | None = None) -> dict[str, Any]:
    """The sampling filters a request gives, validated; {} for none."""
    f = {k: v for k, v in (("area", area), ("mechanism", mechanism), ("environment", environment),
                           ("dial", dial)) if v is not None}
    if "area" in f:
        f["area"] = resolve_area(f["area"])
    return f


def _check_request(prompt: str | None, spec: dict[str, Any] | None, filters: dict[str, Any]) -> None:
    if prompt is not None and spec is not None:
        raise ValueError("give a prompt or a spec, not both")
    if prompt is not None and not prompt.strip():
        raise ValueError("prompt is empty")
    if spec is not None:
        if filters:
            raise ValueError("give area/mechanism/environment/dial filters or an explicit spec, not both")
        if not spec.get("taxonomy") and not spec.get("seed") and not spec.get("family"):
            raise ValueError("spec needs a family, a taxonomy or a seed")


def _explicit_v2(spec: dict[str, Any], existing: list[dict]) -> dict[str, Any]:
    """A spec naming a family: validated, its missing v2 fields sampled within that family."""
    family = spec["family"]
    if family not in FAMILIES:
        raise ValueError(f"unknown family {family!r}; valid families: {sorted(FAMILIES)}")
    fam = FAMILIES[family]
    for key, allowed in (("mechanism", fam.mechanisms), ("environment", fam.environments),
                         ("verification", fam.verification), ("shape", SHAPES)):
        if key in spec and spec[key] not in allowed:
            raise ValueError(f"{key} {spec[key]!r} is not allowed in {family}; allowed: {list(allowed)}")
    filled = sample_spec_v2(existing, area=family, mechanism=spec.get("mechanism"),
                            environment=spec.get("environment"), dial=spec.get("dial"))
    for key in ("verification", "shape"):
        if key in spec:
            filled[key] = spec[key]
    filled.update(legacy_view(filled))  # the v1 view follows any override
    passthrough = {k: v for k, v in spec.items() if k not in V2_FIELDS}  # seed, design, other keys
    return {**filled, **passthrough}


def make_slot(existing: list[dict], prompt: str | None = None, spec: dict[str, Any] | None = None,
              filters: dict[str, Any] | None = None) -> dict[str, Any]:
    """The spec a new task gets. `existing` is every task's slot so far (plus, in a
    batch, the ones already picked for it), the sampler's spread input."""
    filters = filters or {}
    if spec is not None:
        if "family" in spec:
            return _explicit_v2(spec, existing)
        return {**spec, "difficulty": spec.get("difficulty") or DEFAULT_DIFFICULTY}
    seed = {"seed": prompt.strip()} if prompt is not None else {}
    if models.AUTHOR_MODE == "agent" or filters:
        return {**sample_spec_v2(existing, **filters), **seed}
    if seed:
        return {**seed, "difficulty": DEFAULT_DIFFICULTY}
    return sample_spec([s.get("taxonomy") or {} for s in existing])


def _same_request(slot: dict[str, Any], prompt: str | None, spec: dict[str, Any] | None,
                  filters: dict[str, Any]) -> bool:
    """Was this existing task created by (an equivalent of) this request? Sampled
    fields can't be compared, so only what the request pinned down is."""
    if spec is not None:
        if "family" in spec:
            return all(slot.get(k) == v for k, v in spec.items())
        return slot == {**spec, "difficulty": spec.get("difficulty") or DEFAULT_DIFFICULTY}
    if prompt is not None and slot.get("seed") != prompt.strip():
        return False
    if not filters:
        return True
    return ("family" in slot and (not filters.get("area") or slot["family"] in filters["area"])
            and all(slot.get(k) == filters[k] for k in ("mechanism", "environment", "dial") if k in filters))


# --- writing ------------------------------------------------------------------------


def _slot(row: Any) -> dict[str, Any]:
    return row["slot"] if isinstance(row["slot"], dict) else json.loads(row["slot"] or "{}")


def _slots(conn: Any) -> list[dict]:
    """Every task's spec so far (the sampler's spread input)."""
    return [_slot(r) for r in conn.execute("SELECT slot FROM tasks").fetchall()]


# --- reusing checked worlds (docs/HILLCLIMBING_ALGORITHM.md "reuse worlds") ---------------

def _input(row: Any) -> dict[str, Any]:
    v = row["input"]
    return v if isinstance(v, dict) else json.loads(v or "{}")


def world_pool(conn: Any) -> list[dict[str, Any]]:
    """Every world that passed world_check and cost something (a real model built it; mock
    worlds are cost 0), with the task that built it, that task's spec and status, and how many
    tasks have started from it since (reuses)."""
    checked = conn.execute(
        "SELECT j.task_id, j.input, t.slot, t.status, t.reason FROM jobs j JOIN tasks t ON t.id = j.task_id"
        " WHERE j.type = ? AND j.outcome = 'OK' ORDER BY j.finished_at",
        (str(JobType.WORLD_CHECK),)).fetchall()
    paid = {(r["task_id"], _input(r).get("world_sha256")): _input(r).get("name") or "" for r in conn.execute(
        "SELECT task_id, result AS input FROM jobs WHERE type = ? AND outcome = 'OK' AND cost_usd > 0",
        (str(JobType.WORLD),)).fetchall()}
    reuses: dict[str, int] = {}
    for r in conn.execute("SELECT input FROM jobs WHERE type = ?", (str(JobType.CREATE),)).fetchall():
        inp = _input(r)
        if inp.get("origin") == "initial" and inp.get("reused") and inp.get("world_sha256"):
            reuses[inp["world_sha256"]] = reuses.get(inp["world_sha256"], 0) + 1
    pool: dict[str, dict[str, Any]] = {}
    for r in checked:
        sha = _input(r).get("world_sha256")
        if not sha or sha in pool or (r["task_id"], sha) not in paid:
            continue
        slot = _slot(r)
        if "family" not in slot:
            continue
        pool[sha] = {"world_sha256": sha, "source_task": r["task_id"], "spec": slot,
                     "family": slot["family"], "mechanism": slot.get("mechanism"),
                     "source_status": r["status"], "source_reason": r["reason"], "reuses": reuses.get(sha, 0),
                     "name": paid[(r["task_id"], sha)]}
    return list(pool.values())


def pick_worlds(pool: list[dict[str, Any]], count: int, area: Any = None,
                uses: dict[str, list[world_selection.Use]] | None = None,
                greedy: set[str] | None = None) -> list[dict[str, Any]]:
    """`count` worlds for new tasks within `area` (a family or a list): world_selection's policy
    over each world's history (`uses`, from world_selection.world_uses), each pick carrying its
    `tier` and `reason`. Every family's worlds count toward the domain cap. If the policy finds
    too few, ValueError: an accepted, leaked or reuse-capped world is never handed out to fill a batch."""
    areas = [area] if isinstance(area, str) else list(area or [])
    if not any(not areas or w["family"] in areas for w in pool):
        raise ValueError(f"no checked world to reuse{f' in {sorted(areas)}' if areas else ''}")
    families = sorted({w["family"] for w in pool if not areas or w["family"] in areas})
    by_sha = {w["world_sha256"]: dict(w) for w in pool}
    worlds = [world_selection.World(w["world_sha256"], w["family"], w.get("mechanism") or "",
                                    list((uses or {}).get(w["world_sha256"], [])), w["reuses"],
                                    name=w.get("name") or "", greedy=w["world_sha256"] in (greedy or set()))
              for w in pool]
    found = world_selection.select_worlds(worlds, families, count)
    if len(found) < count:
        raise ValueError(f"only {len(found)} of {count} worlds in {sorted(families)} can be reused now "
                         "(the rest have an accepted or leaked task, hit the reuse cap or the domain cap, "
                         "or already share with a live task); ask for fewer")
    picks: list[dict[str, Any]] = []
    for p in found:
        best = by_sha[p.world_sha256]
        best["reuses"] += 1
        picks.append({**best, "tier": p.tier, "reason": p.reason})
    return picks


def _check_reuse(prompt: str | None, spec: dict[str, Any] | None, filters: dict[str, Any]) -> None:
    """A reused world brings its own spec: only `area` may narrow the pick."""
    if prompt is not None or spec is not None or set(filters) - {"area"}:
        raise ValueError("reuse_worlds takes only `area` (the world brings its spec): "
                         "no prompt, spec, mechanism, environment or dial")


def _reused_slot(world: dict[str, Any]) -> dict[str, Any]:
    """A new task's spec from a reused world: the source task's, plus where it came from."""
    spec = {k: v for k, v in world["spec"].items()
            if k not in ("reused_world", "reused_from", "reuse_tier", "reuse_reason")}
    return {**spec, "reused_world": world["world_sha256"], "reused_from": world["source_task"],
            **({"reuse_tier": world["tier"], "reuse_reason": world["reason"]} if "tier" in world else {})}


def first_job(task_id: str, slot: dict[str, Any]) -> tuple[str, JobType, dict[str, Any]]:
    """(job id, type, input) of a new task's first job."""
    if "reused_world" in slot:
        # Exactly what after_world_check hands create for a fresh world (shape / dial defaults as
        # trace_parser_service._world_spec), marked reused for world_pool's count.
        return job_id(task_id, 1, "create"), JobType.CREATE, {
            "origin": "initial", "world_sha256": slot["reused_world"], "shape": slot.get("shape") or "fix",
            "dial": slot.get("dial") or (slot.get("difficulty") or {}).get("dial", 2), "reused": True,
            **{k: slot[k] for k in ("reuse_tier", "reuse_reason") if k in slot}}
    if "raw_prompt" in slot:
        return job_id(task_id, 1, "create"), JobType.CREATE, {"origin": "initial", "prompt": slot["raw_prompt"]}
    if models.AUTHOR_MODE == "agent" and "family" in slot:
        return job_id(task_id, 1, "world"), JobType.WORLD, {"origin": "initial", "spec": slot}
    return job_id(task_id, 1, "create"), JobType.CREATE, {"origin": "initial", "spec": slot}


def _insert_task(conn: Any, task_id: str, slot: dict[str, Any], batch_id: str | None = None) -> str:
    """Insert one task (CREATED, v1 QUEUED) and its first job. Returns the job id."""
    now = utcnow()
    conn.execute(
        """INSERT INTO tasks (id, slot, status, current_version, stage, batch_id, created_at, updated_at)
           VALUES (?, ?, ?, 1, ?, ?, ?, ?)""",
        (task_id, json.dumps(slot, sort_keys=True), str(TaskStatus.CREATED), str(Stage.QUEUED),
         batch_id, now, now),
    )
    jid, jtype, job_input = first_job(task_id, slot)
    insert_job(conn, id=jid, task_id=task_id, version=1, type=jtype, input=job_input)
    return jid


def create_task(backend: Backend, prompt: str | None = None, spec: dict[str, Any] | None = None,
                task_id: str | None = None, raw_prompt: str | None = None, area: Any = None,
                mechanism: str | None = None, environment: str | None = None,
                dial: int | None = None) -> tuple[str, bool]:
    """Create one task and its first job. Returns (task_id, created)."""
    filters = filters_for(area, mechanism, environment, dial)
    if raw_prompt is not None:
        if prompt is not None or spec is not None or filters:
            raise ValueError("a raw_prompt can't be combined with a prompt, a spec or filters")
        if not raw_prompt.strip():
            raise ValueError("raw_prompt is empty")
    else:
        _check_request(prompt, spec, filters)
    task_id = task_id or new_task_id()
    with backend.store.tx() as conn:
        row = conn.execute("SELECT slot FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is not None:
            same = (_slot(row) == {"raw_prompt": raw_prompt} if raw_prompt is not None
                    else _same_request(_slot(row), prompt, spec, filters))
            if not same:
                raise TaskConflict(f"task {task_id!r} already exists with a different request")
            return task_id, False
        slot = {"raw_prompt": raw_prompt} if raw_prompt is not None else make_slot(_slots(conn), prompt, spec, filters)
        jid = _insert_task(conn, task_id, slot)
    JobQueue(backend).publish([jid])
    return task_id, True


def create_batch(backend: Backend, count: int, batch_id: str | None = None, prompt: str | None = None,
                 spec: dict[str, Any] | None = None, area: Any = None, mechanism: str | None = None,
                 environment: str | None = None, dial: int | None = None,
                 budget_usd: float | None = None, reuse_worlds: bool = False) -> tuple[str, list[str], bool]:
    """Create `count` tasks labelled with one batch id. Returns (batch_id, task_ids, created).

    `reuse_worlds`: each task starts at create on a checked world (pick_worlds, within `area`)
    instead of building one; a prompt, a spec or the other filters can't be combined with it.

    `budget_usd` caps what the batch's jobs may spend (shared/storage/budget.py): no paid job of
    the batch is claimed once its spend would pass it.

    One transaction: the batch exists whole or not at all. Tasks are `<batch_id>-tNN`.
    Each task's spec is made separately, spread across every task so far plus those
    already picked for this batch, within `area` and the filters if given (a prompt
    becomes every task's seed). A v1 spec or a plain prompt in one-shot mode applies to
    every task as given. The same batch id again with the same count and request is a
    no-op; anything else is a conflict.
    """
    if not 1 <= count <= MAX_BATCH:
        raise ValueError(f"count must be 1-{MAX_BATCH}")
    if budget_usd is not None and budget_usd < 0:
        raise ValueError("budget_usd must be >= 0")
    filters = filters_for(area, mechanism, environment, dial)
    if reuse_worlds:
        _check_reuse(prompt, spec, filters)
    else:
        _check_request(prompt, spec, filters)
    batch_id = batch_id or new_batch_id()
    task_ids = [f"{batch_id}-t{i:02d}" for i in range(1, count + 1)]
    with backend.store.tx() as conn:
        rows = conn.execute("SELECT id, slot FROM tasks WHERE batch_id = ? ORDER BY id", (batch_id,)).fetchall()
        if rows:
            had = conn.execute("SELECT budget_usd FROM batches WHERE id = ?", (batch_id,)).fetchone()
            same = (all(("reused_world" in _slot(r)) == reuse_worlds for r in rows) and
                    (reuse_worlds or all(_same_request(_slot(r), prompt, spec, filters) for r in rows)))
            if (len(rows) != count or not same
                    or (had["budget_usd"] if had else None) != budget_usd):
                raise BatchConflict(f"batch {batch_id!r} already exists with a different request")
            return batch_id, [r["id"] for r in rows], False
        marks = ",".join("?" * count)
        if conn.execute(f"SELECT 1 FROM tasks WHERE id IN ({marks})", task_ids).fetchone():
            raise BatchConflict(f"a task id of batch {batch_id!r} is already taken")
        conn.execute("INSERT INTO batches (id, budget_usd, created_at) VALUES (?, ?, ?)",
                     (batch_id, budget_usd, utcnow()))
        jids = []
        if reuse_worlds:
            pool, area = world_pool(conn), filters.get("area")
            areas = [area] if isinstance(area, str) else list(area or [])
            greedy = world_selection.greedy_worlds(
                backend.artifacts, [w["world_sha256"] for w in pool if not areas or w["family"] in areas])
            picks = pick_worlds(pool, count, area, world_selection.world_uses(conn), greedy)
            for tid, world in zip(task_ids, picks):
                jids.append(_insert_task(conn, tid, _reused_slot(world), batch_id))
        else:
            existing = _slots(conn)
            for tid in task_ids:
                slot = make_slot(existing, prompt, spec, filters)
                existing.append(slot)
                jids.append(_insert_task(conn, tid, slot, batch_id))
    JobQueue(backend).publish(jids)
    return batch_id, task_ids, True
