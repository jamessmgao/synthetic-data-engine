"""
Task state changes, shared by every service that owns a stage (PRD §5.1).

Each change is a compare-and-set on the task's version and stage (or status),
run inside the caller's transaction, usually the settle of the job that
triggered it. If the task moved on in the meantime (another copy got there
first, or the task was decided), the CAS misses and the caller does nothing
else. That is what lets any number of copies of each service run at once.

A decided task (ACCEPTED, REJECTED, or CANCELLED by a user) can't use its remaining open jobs: they are
closed in the same transaction, as DEAD with a `cancelled:` error, so nothing
is left queued or running for it. A worker still holding one loses its fenced
heartbeat or settle and stops.
"""

from typing import Any

from shared.storage.sql import Conn, utcnow
from shared.contracts.models import CANCELLED_REASON, RejectReason, Stage, TaskStatus

OPEN = (TaskStatus.CREATED, TaskStatus.CALIBRATING)


def task(conn: Conn, task_id: str) -> dict[str, Any] | None:
    return conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()


def cas(conn: Conn, task_id: str, *, version: int, stages: tuple[Stage, ...] | None = None,
        **fields: Any) -> bool:
    """Set `fields` on an open task still at `version` (and one of `stages`). True if it did."""
    fields = {k: str(v) if hasattr(v, "value") else v for k, v in fields.items()}
    fields["updated_at"] = utcnow()
    sets = ", ".join(f"{k} = ?" for k in fields)
    where = "id = ? AND current_version = ? AND status IN (?, ?)"
    params: list[Any] = [*fields.values(), task_id, version, *map(str, OPEN)]
    if stages:
        where += f" AND stage IN ({','.join('?' * len(stages))})"
        params += [str(s) for s in stages]
    return conn.execute(f"UPDATE tasks SET {sets} WHERE {where}", params).rowcount == 1


def start(conn: Conn, task_id: str, version: int, stage: Stage, from_stages: tuple[Stage, ...]) -> bool:
    """A service began work on the version: CREATED → CALIBRATING, and the new stage."""
    conn.execute(
        "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ? AND status = ?",
        (str(TaskStatus.CALIBRATING), utcnow(), task_id, str(TaskStatus.CREATED)),
    )
    return cas(conn, task_id, version=version, stages=from_stages, stage=stage)


def close_open_jobs(conn: Conn, task_id: str, why: str) -> int:
    now = utcnow()
    return conn.execute(
        """UPDATE jobs SET status = 'DEAD', finished_at = ?, lease_until = NULL, handled_at = ?,
                           error = ?
            WHERE task_id = ? AND status IN ('READY', 'LEASED')""",
        (now, now, f"cancelled: {why}", task_id),
    ).rowcount


def reject(conn: Conn, task_id: str, reason: RejectReason, stage: Stage, *, version: int | None = None,
           stages: tuple[Stage, ...] | None = None) -> bool:
    """REJECTED with `reason`, if the task is still open (at `version` / `stages` if given)."""
    fields = {"status": TaskStatus.REJECTED, "reason": reason, "stage": stage}
    if version is None:
        row = task(conn, task_id)
        if row is None:
            return False
        version = row["current_version"]
    if not cas(conn, task_id, version=version, stages=stages, **fields):
        return False
    close_open_jobs(conn, task_id, f"task REJECTED ({reason})")
    return True


def cancel(conn: Conn, task_id: str, by: str, note: str = "") -> dict[str, Any]:
    """A user stops the task: CANCELLED (from any open status, version or stage), and its
    READY / LEASED jobs closed. Every later compare-and-set on it misses, so a job still
    running settles into nothing (no hand-off, nothing enqueued; its cost is still
    recorded), and no job of it is claimed again. Idempotent.
    {task_id, cancelled: did this call cancel it, status: the task's status now, closed_jobs}."""
    now = utcnow()
    changed = conn.execute(
        f"""UPDATE tasks SET status = ?, stage = ?, reason = ?, cancelled_at = ?, cancelled_by = ?,
                            cancel_note = ?, updated_at = ?
             WHERE id = ? AND status IN ({','.join('?' * len(OPEN))})""",
        (str(TaskStatus.CANCELLED), str(Stage.CANCELLED), CANCELLED_REASON, now, by, note or None, now,
         task_id, *map(str, OPEN)),
    ).rowcount == 1
    closed = close_open_jobs(conn, task_id, f"task CANCELLED by {by}") if changed else 0
    row = task(conn, task_id)
    return {"task_id": task_id, "cancelled": changed, "status": row["status"] if row else None,
            "closed_jobs": closed}


def relabel_as_cancelled(conn: Conn, task_id: str, by: str, note: str = "",
                         from_reason: RejectReason = RejectReason.INFRA_UNRESOLVED) -> bool:
    """A task an operator stopped by rejecting it (before cancel existed) becomes what it
    was: CANCELLED. Only a REJECTED task with `from_reason`; its jobs are already closed.
    The original decision time is kept as cancelled_at."""
    return conn.execute(
        """UPDATE tasks SET status = ?, stage = ?, reason = ?, cancelled_at = updated_at, cancelled_by = ?,
                            cancel_note = ?, updated_at = ?
            WHERE id = ? AND status = ? AND reason = ?""",
        (str(TaskStatus.CANCELLED), str(Stage.CANCELLED), CANCELLED_REASON, by,
         note or f"relabelled from REJECTED ({from_reason})", utcnow(), task_id, str(TaskStatus.REJECTED),
         str(from_reason)),
    ).rowcount == 1


def accept(conn: Conn, task_id: str, version: int) -> bool:
    """ACCEPTED at `version`, if the version is still being evaluated."""
    if not cas(conn, task_id, version=version, stages=(Stage.EVALUATING,),
               status=TaskStatus.ACCEPTED, stage=Stage.BAND_OK, accepted_version=version):
        return False
    close_open_jobs(conn, task_id, "task ACCEPTED")
    return True
