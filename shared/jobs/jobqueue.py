"""The worker contract: claim / heartbeat / settle / release (PRD §6.7).

Every worker (generation, build+validate, executor) pulls its own jobs through a
`JobQueue` and reports results with `settle`. Workers never change task state:
the Job Service reads settled jobs (`unhandled()`) and decides what happens next.

Guarantees, on SQLite and PostgreSQL alike:

- A job is leased to one worker at a time; each claim bumps its `lease_token`.
- `heartbeat`, `settle` and `release` are compare-and-set on that token (fencing),
  so a worker whose lease was taken over (it paused, or was presumed dead) changes
  nothing.
- Follow-up jobs passed to `settle` are inserted in the same transaction, by
  deterministic id, so a retried settle can't create them twice.
- New jobs go to the queue backend (Redis) only after their transaction commits;
  the reconciler re-adds anything lost in between.
"""

import json
import os
import socket
import time
from typing import Any

from shared.storage import Backend
from shared.storage.sql import claim as sql_claim, insert_job, utcnow
from shared.contracts.models import LEASE_TTL_S, JobStatus, JobType

# Columns a worker may set when it settles a job.
SETTLE_COLUMNS = (
    "outcome", "result", "reward", "error", "trace_uri", "trace_metrics", "failure",
    "cost_usd", "bundle_sha256", "config_hash",
)
_JSON_COLUMNS = ("result", "trace_metrics", "failure")


class JobQueue:
    def __init__(self, backend: Backend):
        self.store = backend.store
        self.queue = backend.queue

    def claim(
        self,
        job_type: str | JobType | list[str | JobType],
        owner: str,
        lease_sec: float = LEASE_TTL_S,
        docker_slots: int | None = None,
        batch_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Lease the best claimable job of the given type(s), or return None.

        `docker_slots` caps leased validate + rollout jobs across all processes.
        The returned row includes `lease_token`; pass it to every later call.

        Owners named `<host>:<pid>:…` (both worker loops do this) get crash
        isolation: see `sql.claim`.
        """
        types = [JobType(t) for t in (job_type if isinstance(job_type, list) else [job_type])]
        process = process_prefix()
        process = process if owner.startswith(process) else None
        for candidate in self.queue.candidates(types):
            with self.store.tx() as c:
                row = sql_claim(c, owner, types, lease_s=lease_sec, docker_slots=docker_slots,
                                batch_id=batch_id, only_id=candidate, process=process)
            if row:
                if candidate:
                    self.queue.claimed(candidate)
                return row
            if candidate:
                self.queue.missed(candidate)
        return None

    def heartbeat(self, job_id: str, token: int, lease_sec: float = LEASE_TTL_S) -> bool:
        """Extend the lease. False means another worker took the job over: stop work."""
        cur = self.store.conn.execute(
            "UPDATE jobs SET lease_until = ? WHERE id = ? AND lease_token = ? AND status = 'LEASED'",
            (time.time() + lease_sec, job_id, token),
        )
        return cur.rowcount == 1

    def settle(
        self,
        job_id: str,
        token: int,
        fields: dict[str, Any],
        followups: tuple[dict[str, Any], ...] = (),
    ) -> bool:
        """Record a finished job and create its follow-ups, atomically.

        `fields` holds any of SETTLE_COLUMNS, plus optionally `status: "DEAD"` (the
        default is DONE). `followups` are job rows (`id`, `type`, `task_id`,
        `version`, and optionally `slot`, `retry`, `input`, `delay_sec`, `priority`,
        `bundle_sha256`, `config_hash`); missing pinning fields are copied from the
        settled job. Returns False, and changes nothing, if the lease was lost.
        """
        status = fields.get("status", JobStatus.DONE)
        if status not in (JobStatus.DONE, JobStatus.DEAD):
            raise ValueError(f"cannot settle a job as {status}")
        unknown = set(fields) - set(SETTLE_COLUMNS) - {"status"}
        if unknown:
            raise ValueError(f"unknown settle fields: {sorted(unknown)}")
        values = {k: fields[k] for k in SETTLE_COLUMNS if k in fields}
        for k in _JSON_COLUMNS:
            if values.get(k) is not None:
                values[k] = json.dumps(values[k])
        sets = "".join(f", {k} = ?" for k in values)
        new_ids: list[str] = []
        with self.store.tx() as c:
            cur = c.execute(
                f"""UPDATE jobs SET status = ?, finished_at = ?, lease_until = NULL {sets}
                     WHERE id = ? AND lease_token = ? AND status = 'LEASED'""",
                (str(status), utcnow(), *values.values(), job_id, token),
            )
            if cur.rowcount != 1:
                return False
            parent = c.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            for f in followups:
                if _insert_followup(c, f, parent):
                    new_ids.append(f["id"])
        self.publish(new_ids)
        return True

    def release(
        self, job_id: str, token: int, error: str | None = None, delay_sec: float = 0.0
    ) -> bool:
        """Give a job back unfinished (shutdown, or the handler crashed)."""
        with self.store.tx() as c:
            cur = c.execute(
                """UPDATE jobs SET status = 'READY', lease_owner = NULL, lease_until = NULL,
                       due_at = ?, error = COALESCE(?, error), published_at = NULL
                    WHERE id = ? AND lease_token = ? AND status = 'LEASED'""",
                (time.time() + delay_sec, error, job_id, token),
            )
            ok = cur.rowcount == 1
        if ok:
            self.publish([job_id])
        return ok

    def publish(self, job_ids: list[str]) -> None:
        """Hand committed jobs to the queue backend. Call after the inserting
        transaction commits (a no-op for the database queue)."""
        if not job_ids:
            return
        marks = ",".join("?" * len(job_ids))
        rows = self.store.conn.execute(
            f"SELECT id, type, due_at FROM jobs WHERE id IN ({marks})", job_ids
        ).fetchall()
        self.queue.publish(rows)


def process_prefix() -> str:
    """The owner prefix every thread of this process shares."""
    return f"{socket.gethostname()}:{os.getpid()}:"


def _insert_followup(c, f: dict[str, Any], parent: dict[str, Any]) -> bool:
    parent_input = json.loads(parent["input"]) if isinstance(parent["input"], str) else parent["input"]
    return insert_job(
        c, id=f["id"], task_id=f["task_id"], version=f["version"], type=JobType(f["type"]),
        input=f.get("input") or parent_input, slot=f.get("slot", 0), retry=f.get("retry", 0),
        bundle_sha256=f.get("bundle_sha256") or parent["bundle_sha256"],
        config_hash=f.get("config_hash") or parent["config_hash"],
        delay_s=f.get("delay_sec", 0.0), priority=f.get("priority") or None,
    )
