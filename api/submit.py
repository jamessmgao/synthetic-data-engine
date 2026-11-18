"""
Task creation: the API service's only write (PRD §4, §5.2 step 1).

One transaction creates the task (CREATED, v1 QUEUED) and its `create` job, whose
input carries the task's spec. A batch (`create_batch`) is N such tasks in one
transaction under a shared `batch_id` label, so their yield can be reported together;
nothing downstream knows about batches.

The job row is the outbox: it reaches the create queue after the commit. From
there the Generation Service takes over; nothing here waits on it.

The spec comes from one of three requests:

- a text **prompt**: the idea the task is built around. It seeds the standard
  authoring prompt (quality rules and difficulty targets still apply);
- an explicit **spec**: `taxonomy` and/or `difficulty` fields, missing
  difficulty filled with the default profile;
- **nothing**: a taxonomy slot is sampled, least-used across every task so far;
- a **raw prompt**: the complete user message, sent to GLM-5.1 verbatim instead of
  the template (for prompt experiments). The system prompt and the response schema
  still apply, so every rule the gates check is unchanged. The task's slot records
  it as `{"raw_prompt": ...}`: it bypasses the sampler and its diversity spread.

An optional client `task_id` makes a retry safe: the same id with the same
request inserts nothing; the same id with a different request is a conflict.
"""

import json
import secrets
from datetime import UTC, datetime
from typing import Any

from shared.storage import Backend
from shared.storage.sql import insert_job, job_id, utcnow
from shared.jobs.jobqueue import JobQueue
from shared.contracts.models import JobType, Stage, TaskStatus
from shared.taxonomy.taxonomy import DEFAULT_DIFFICULTY, sample_spec


MAX_BATCH = 50


class TaskConflict(Exception):
    """The task id exists with a different request."""


class BatchConflict(Exception):
    """The batch id exists with a different request."""


def new_task_id() -> str:
    return f"t-{datetime.now(UTC):%Y%m%d}-{secrets.token_hex(4)}"


def new_batch_id() -> str:
    return f"b-{datetime.now(UTC):%Y%m%d}-{secrets.token_hex(4)}"


def spec_for(prompt: str | None = None, spec: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """The spec a request asks for, or None to sample one."""
    if prompt is not None and spec is not None:
        raise ValueError("give a prompt or a spec, not both")
    if prompt is not None:
        if not prompt.strip():
            raise ValueError("prompt is empty")
        return {"seed": prompt.strip(), "difficulty": DEFAULT_DIFFICULTY}
    if spec is not None:
        if not spec.get("taxonomy") and not spec.get("seed"):
            raise ValueError("spec needs a taxonomy or a seed")
        return {**spec, "difficulty": spec.get("difficulty") or DEFAULT_DIFFICULTY}
    return None


def _slot(row: Any) -> dict[str, Any]:
    return row["slot"] if isinstance(row["slot"], dict) else json.loads(row["slot"] or "{}")


def _taxonomies(conn: Any) -> list[dict]:
    """The taxonomy of every task so far (the sampler's spread input)."""
    return [_slot(r).get("taxonomy") or {} for r in conn.execute("SELECT slot FROM tasks").fetchall()]


def _insert_task(conn: Any, task_id: str, slot: dict[str, Any], batch_id: str | None = None) -> str:
    """Insert one task (CREATED, v1 QUEUED) and its `create` job. Returns the job id."""
    now = utcnow()
    conn.execute(
        """INSERT INTO tasks (id, slot, status, current_version, stage, batch_id, created_at, updated_at)
           VALUES (?, ?, ?, 1, ?, ?, ?, ?)""",
        (task_id, json.dumps(slot, sort_keys=True), str(TaskStatus.CREATED), str(Stage.QUEUED),
         batch_id, now, now),
    )
    job_input = ({"origin": "initial", "prompt": slot["raw_prompt"]} if "raw_prompt" in slot
                 else {"origin": "initial", "spec": slot})
    jid = job_id(task_id, 1, "create")
    insert_job(conn, id=jid, task_id=task_id, version=1, type=JobType.CREATE, input=job_input)
    return jid


def create_task(backend: Backend, prompt: str | None = None, spec: dict[str, Any] | None = None,
                task_id: str | None = None, raw_prompt: str | None = None) -> tuple[str, bool]:
    """Create one task and its v1 `create` job. Returns (task_id, created)."""
    if raw_prompt is not None:
        if prompt is not None or spec is not None:
            raise ValueError("give one of prompt, spec or raw_prompt")
        if not raw_prompt.strip():
            raise ValueError("raw_prompt is empty")
        wanted: dict[str, Any] | None = {"raw_prompt": raw_prompt}
    else:
        wanted = spec_for(prompt, spec)
    task_id = task_id or new_task_id()
    with backend.store.tx() as conn:
        row = conn.execute("SELECT slot FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is not None:
            if wanted is not None and _slot(row) != wanted:
                raise TaskConflict(f"task {task_id!r} already exists with a different request")
            return task_id, False
        jid = _insert_task(conn, task_id, wanted or sample_spec(_taxonomies(conn)))
    JobQueue(backend).publish([jid])
    return task_id, True


def create_batch(backend: Backend, count: int, batch_id: str | None = None, prompt: str | None = None,
                 spec: dict[str, Any] | None = None) -> tuple[str, list[str], bool]:
    """Create `count` tasks labelled with one batch id. Returns (batch_id, task_ids, created).

    One transaction: the batch exists whole or not at all. Tasks are `<batch_id>-tNN`.
    A prompt or spec applies to every task; otherwise each samples its own spec, spread
    across every task so far plus those already picked for this batch. The same
    batch id again with the same count (and request) is a no-op; anything else is a conflict.
    """
    if not 1 <= count <= MAX_BATCH:
        raise ValueError(f"count must be 1-{MAX_BATCH}")
    wanted = spec_for(prompt, spec)
    batch_id = batch_id or new_batch_id()
    task_ids = [f"{batch_id}-t{i:02d}" for i in range(1, count + 1)]
    with backend.store.tx() as conn:
        rows = conn.execute("SELECT id, slot FROM tasks WHERE batch_id = ? ORDER BY id", (batch_id,)).fetchall()
        if rows:
            if len(rows) != count or (wanted is not None and any(_slot(r) != wanted for r in rows)):
                raise BatchConflict(f"batch {batch_id!r} already exists with a different request")
            return batch_id, [r["id"] for r in rows], False
        marks = ",".join("?" * count)
        if conn.execute(f"SELECT 1 FROM tasks WHERE id IN ({marks})", task_ids).fetchone():
            raise BatchConflict(f"a task id of batch {batch_id!r} is already taken")
        existing = _taxonomies(conn)
        jids = []
        for tid in task_ids:
            slot = wanted or sample_spec(existing)
            existing.append(slot.get("taxonomy") or {})
            jids.append(_insert_task(conn, tid, slot, batch_id))
    JobQueue(backend).publish(jids)
    return batch_id, task_ids, True
