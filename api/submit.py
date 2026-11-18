"""
Batch submission: the API service's only write (PRD §4, §5.2 step 1).

One transaction creates the batch's tasks (CREATED, v1 QUEUED) and one `create`
job each, carrying the task's taxonomy slot and difficulty profile as the spec.
The job rows are the outbox: they reach the create queue after the commit.
From there the Generation Service takes over; nothing here waits on it.

Idempotent on `batch_id`: task ids, specs and job ids are deterministic, so
repeating a submission inserts nothing.
"""

import json
import secrets
from datetime import UTC, datetime

from shared.storage import Backend
from shared.storage.sql import insert_job, job_id, utcnow
from shared.jobs.jobqueue import JobQueue
from shared.contracts.models import JobType, Stage, TaskStatus
from shared.taxonomy.taxonomy import sample_specs

MAX_BATCH_COUNT = 50


class BatchConflict(Exception):
    """The batch id exists with a different task count."""


def new_batch_id() -> str:
    return f"b-{datetime.now(UTC):%Y%m%d}-{secrets.token_hex(3)}"


def task_ids_for(batch_id: str, count: int) -> list[str]:
    return [f"{batch_id}-t{i:02d}" for i in range(1, count + 1)]


def submit_batch(backend: Backend, count: int, batch_id: str | None = None) -> tuple[str, list[str], bool]:
    """Create `count` tasks and their v1 `create` jobs. Returns (batch_id, task_ids, created)."""
    if not 1 <= count <= MAX_BATCH_COUNT:
        raise ValueError(f"count must be 1–{MAX_BATCH_COUNT}")
    batch_id = batch_id or new_batch_id()
    task_ids = task_ids_for(batch_id, count)
    specs = sample_specs(batch_id, count)
    new_jobs = []
    with backend.store.tx() as conn:
        existing = conn.execute(
            "SELECT COUNT(*) AS n FROM tasks WHERE batch_id = ?", (batch_id,)
        ).fetchone()["n"]
        if existing and existing != count:
            raise BatchConflict(f"batch {batch_id!r} already has {existing} tasks, not {count}")
        now = utcnow()
        for task_id, spec in zip(task_ids, specs):
            conn.execute(
                """INSERT INTO tasks (id, batch_id, slot, status, current_version, stage,
                                      created_at, updated_at)
                   VALUES (?, ?, ?, ?, 1, ?, ?, ?) ON CONFLICT DO NOTHING""",
                (task_id, batch_id, json.dumps(spec, sort_keys=True), str(TaskStatus.CREATED),
                 str(Stage.QUEUED), now, now),
            )
            jid = job_id(task_id, 1, "create")
            if insert_job(conn, id=jid, task_id=task_id, version=1, type=JobType.CREATE,
                          input={"origin": "initial", "spec": spec}):
                new_jobs.append(jid)
    JobQueue(backend).publish(new_jobs)
    return batch_id, task_ids, existing == 0
