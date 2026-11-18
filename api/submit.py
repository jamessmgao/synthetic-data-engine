"""
Task creation: the API service's only write (PRD §4, §5.2 step 1).

One task per call; the system has no batches. One transaction creates the task
(CREATED, v1 QUEUED) and its `create` job, whose input carries the task's spec.
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


class TaskConflict(Exception):
    """The task id exists with a different request."""


def new_task_id() -> str:
    return f"t-{datetime.now(UTC):%Y%m%d}-{secrets.token_hex(4)}"


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
    jid = job_id(task_id, 1, "create")
    with backend.store.tx() as conn:
        row = conn.execute("SELECT slot FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is not None:
            stored = row["slot"] if isinstance(row["slot"], dict) else json.loads(row["slot"] or "{}")
            if wanted is not None and stored != wanted:
                raise TaskConflict(f"task {task_id!r} already exists with a different request")
            return task_id, False
        if wanted is None:
            existing = [json.loads(r["slot"] or "{}").get("taxonomy") or {}
                        for r in conn.execute("SELECT slot FROM tasks").fetchall()]
            wanted = sample_spec(existing)
        now = utcnow()
        conn.execute(
            """INSERT INTO tasks (id, slot, status, current_version, stage, created_at, updated_at)
               VALUES (?, ?, ?, 1, ?, ?, ?)""",
            (task_id, json.dumps(wanted, sort_keys=True), str(TaskStatus.CREATED), str(Stage.QUEUED), now, now),
        )
        job_input = ({"origin": "initial", "prompt": raw_prompt} if raw_prompt is not None
                     else {"origin": "initial", "spec": wanted})
        insert_job(conn, id=jid, task_id=task_id, version=1, type=JobType.CREATE, input=job_input)
    JobQueue(backend).publish([jid])
    return task_id, True
