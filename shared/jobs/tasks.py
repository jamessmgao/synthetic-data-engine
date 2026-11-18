"""
Task state changes, shared by every service that owns a stage (PRD §5.1).

Each change is a compare-and-set on the task's version and stage (or status),
run inside the caller's transaction, usually the settle of the job that
triggered it. If the task moved on in the meantime (another copy got there
first, or the task was decided), the CAS misses and the caller does nothing
else. That is what lets any number of copies of each service run at once.

A decided task (ACCEPTED or REJECTED) can't use its remaining open jobs: they are
closed in the same transaction, as DEAD with a `cancelled:` error, so nothing
is left queued or running for it. A worker still holding one loses its fenced
heartbeat or settle and stops.
"""

from typing import Any

from shared.storage.sql import Conn, utcnow
from shared.contracts.models import RejectReason, Stage, TaskStatus

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


def accept(conn: Conn, task_id: str, version: int) -> bool:
    """ACCEPTED at `version`, if the version is still being evaluated."""
    if not cas(conn, task_id, version=version, stages=(Stage.EVALUATING,),
               status=TaskStatus.ACCEPTED, stage=Stage.BAND_OK, accepted_version=version):
        return False
    close_open_jobs(conn, task_id, "task ACCEPTED")
    return True
