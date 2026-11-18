"""The worker contract: claim / heartbeat / settle / release (PRD §6.7).

Every service (Generation Service, Build + Validate, Executor, Trace Parser) pulls
its own jobs through a `JobQueue` and reports results with `settle`. Its hand-off
to the next stage (a task change plus the next stage's jobs) runs inside that
same settle transaction (`then`), so there is no central orchestrator (PRD §5).

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
import logging
import os
import socket
import time
from pathlib import Path
from collections.abc import Callable
from typing import Any

from shared.storage import Backend
from shared.storage.sql import Conn, claim as sql_claim, docker_load, insert_job, row_dict, utcnow
from shared.storage.budget import PAID_JOB_TYPES, warn_if_exhausted
from shared.contracts.models import DOCKER_JOB_TYPES, LEASE_TTL_S, JobStatus, JobType

# (conn, settled job row) -> follow-up jobs; runs inside the settle transaction.
Handoff = Callable[[Conn, dict[str, Any]], list[dict[str, Any]]]

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
        agent_slots: int | None = None,
    ) -> dict[str, Any] | None:
        """Lease the best claimable job of the given type(s), or return None.

        `docker_slots` caps the jobs holding a Docker slot on this machine (jobs whose
        needs_docker is set), `agent_slots` the long agent jobs among them. The
        returned row includes `lease_token`; pass it to every later call.

        Owners named `<host>:<pid>:…` (both worker loops do this) get a per-machine
        Docker cap and crash isolation: see `sql.claim`. Other owners share one
        global cap.
        """
        types = [JobType(t) for t in (job_type if isinstance(job_type, list) else [job_type])]
        process = process_prefix()
        mine = owner.startswith(process)
        host = f"{socket.gethostname()}:" if mine else None
        types, docker_slots = self._unblocked(types, host, docker_slots, agent_slots)
        if not types:
            return None
        for candidate in self.queue.candidates(types):
            with self.store.tx() as c:
                row = sql_claim(c, owner, types, lease_s=lease_sec, docker_slots=docker_slots,
                                agent_slots=agent_slots, only_id=candidate,
                                process=process if mine else None, host=host)
            if row:
                if candidate:
                    self.queue.claimed(candidate)
                if row.get("needs_docker"):
                    _last_docker_claim[0] = time.monotonic()
                return row
            if candidate:
                self.queue.missed(candidate)
        if any(t in PAID_JOB_TYPES for t in types):
            warn_if_exhausted(self.store)  # paid claims stop at SDE_BUDGET_USD: say so, loudly
        return None

    def _unblocked(self, types: list[JobType], host: str | None, docker_slots: int | None,
                   agent_slots: int | None) -> tuple[list[JobType], int | None]:
        """The types worth trying now, and the Docker slot cap to claim with.

        Counted once per poll (outside the claim lock): with long (20-30 min) agent jobs
        holding the slots, trying every candidate in a locked claim transaction only
        to be refused would churn the database. Types that need Docker only for some
        jobs (create / revise) stay: the claim itself still checks each job.

        The slots are a ceiling; the machine is the real limit. Below
        SDE_MIN_FREE_MEMORY_PCT free memory, or at SDE_MAX_CPU_LOAD_PCT load average, no new
        Docker work is claimed (cap 0): a runner fills up to about 80% and stops there
        instead of OOM-killing trials or starving the solver harness (terminus-2's
        tmux send-keys timed out at 8 rollouts on 2 vCPU)."""
        if docker_slots is None and agent_slots is None:
            return types, docker_slots
        free, cpu = free_memory_pct(), cpu_load_pct()
        low_memory = free is not None and free < MIN_FREE_MEMORY_PCT
        high_load = cpu is not None and cpu >= MAX_CPU_LOAD_PCT
        if docker_slots is not None and (low_memory or high_load):
            _note_guard("memory" if low_memory else "cpu", free, cpu)
            return [t for t in types if t not in DOCKER_JOB_TYPES], 0
        if docker_slots is not None and time.monotonic() - _last_docker_claim[0] < DOCKER_CLAIM_SPACING_S:
            # Pace Docker claims: the load average lags a burst of builds by a minute,
            # so without this one fast node took 8 jobs at once while others sat idle.
            types = [t for t in types if t not in DOCKER_JOB_TYPES]
            if not types:
                return types, docker_slots
        load = docker_load(self.store.conn, host)
        if docker_slots is not None and load["docker"] >= docker_slots:
            return [t for t in types if t not in DOCKER_JOB_TYPES], docker_slots
        if agent_slots is not None and load["agent"] >= agent_slots:
            return [t for t in types if t != JobType.WORLD], docker_slots
        return types, docker_slots

    def heartbeat(self, job_id: str, token: int, lease_sec: float = LEASE_TTL_S) -> bool:
        """Extend the lease. False means another worker took the job over: stop work.
        Uses the store's heartbeat connection, never queued behind the main pool."""
        cur = self.store.heartbeat_conn.execute(
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
        then: Handoff | None = None,
    ) -> bool:
        """Record a finished job and create its follow-ups, atomically.

        `fields` holds any of SETTLE_COLUMNS, plus optionally `status: "DEAD"` (the
        default is DONE). `followups` are job rows (`id`, `type`, `task_id`,
        `version`, and optionally `slot`, `retry`, `input`, `delay_sec`, `priority`,
        `bundle_sha256`, `config_hash`); missing pinning fields are copied from the
        settled job. Returns False, and changes nothing, if the lease was lost.

        `then(conn, settled_row)` is the hand-off to the next stage (PRD §5): it runs
        inside the same transaction, only if the fence held, may change the task
        (compare-and-set) and returns more follow-ups. The result, the task change
        and the next stage's jobs commit together or not at all.
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
                # The task was cancelled (or decided) while this ran: its job was closed, so
                # the result is dropped and nothing is handed off. What it spent is still
                # recorded, so the reports and the budget see the money.
                if values.get("cost_usd") is not None:
                    c.execute("""UPDATE jobs SET cost_usd = ? WHERE id = ? AND lease_token = ? AND status = 'DEAD'
                                    AND error LIKE 'cancelled:%' AND cost_usd IS NULL""",
                              (values["cost_usd"], job_id, token))
                return False
            parent = row_dict(c.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone())
            more = then(c, parent) if then is not None else []
            for f in (*followups, *more):
                if _insert_followup(c, f, parent):
                    new_ids.append(f["id"])
        self.publish(new_ids)
        return True

    def release(
        self, job_id: str, token: int, error: str | None = None, delay_sec: float = 0.0,
        refund_attempt: bool = False,
    ) -> bool:
        """Give a job back unfinished (shutdown, or the handler crashed).

        `refund_attempt` for a deliberate stop (a worker shutting down, e.g. a KEDA scale-
        down): the job wasn't at fault, so its claim shouldn't use up one of its
        MAX_JOB_ATTEMPTS. Batch aws-agent-b1 dead-lettered a healthy rollout whose pod was
        scaled away three times. A crash keeps counting, so a poison job still ends."""
        with self.store.tx() as c:
            cur = c.execute(
                """UPDATE jobs SET status = 'READY', lease_owner = NULL, lease_until = NULL,
                       due_at = ?, error = COALESCE(?, error), published_at = NULL,
                       attempts = CASE WHEN ? AND attempts > 0 THEN attempts - 1 ELSE attempts END
                    WHERE id = ? AND lease_token = ? AND status = 'LEASED'""",
                (time.time() + delay_sec, error, refund_attempt, job_id, token),
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


log = logging.getLogger("jobqueue")
MIN_FREE_MEMORY_PCT = float(os.environ.get("SDE_MIN_FREE_MEMORY_PCT", "20"))
# A backstop, not the limit (the slot ceilings are set from load-test data): the
# 1-minute load average, not instant CPU %, which image builds make too bursty.
MAX_CPU_LOAD_PCT = float(os.environ.get("SDE_MAX_CPU_LOAD_PCT", "90"))
_guard_logged: dict[str, float] = {}
# Seconds between one process's Docker claims (0 = off; the AWS deploy sets 10).
DOCKER_CLAIM_SPACING_S = float(os.environ.get("SDE_DOCKER_CLAIM_SPACING_S", "0"))
_last_docker_claim = [-1e9]


def _note_guard(reason: str, free: float | None, cpu: float | None) -> None:
    """Log a guard skip, at most once a minute per reason (grep 'guard:' in the load test)."""
    now = time.monotonic()
    if now - _guard_logged.get(reason, -1e9) >= 60:
        _guard_logged[reason] = now
        log.warning("guard: %s, no new Docker work (free memory %s%%, load %s%%)", reason,
                    None if free is None else round(free), None if cpu is None else round(cpu))


def cpu_load_pct(loadavg: str = "/proc/loadavg") -> float | None:
    """The 1-minute load average as a percentage of the CPUs (from inside a container,
    /proc/loadavg is the host's), or None where unknown (not Linux: no guard)."""
    try:
        with open(loadavg) as f:
            return 100.0 * float(f.read().split()[0]) / (os.cpu_count() or 1)
    except (OSError, ValueError, IndexError):
        return None


def free_memory_pct(meminfo: str = "/proc/meminfo") -> float | None:
    """Available memory as a percentage of the machine's, or None where unknown (not
    Linux). Inside a container /proc/meminfo shows the host's memory, which is what
    the task containers share."""
    try:
        fields = dict(line.split(":", 1) for line in Path(meminfo).read_text().splitlines() if ":" in line)
        total = float(fields["MemTotal"].split()[0])
        available = float(fields["MemAvailable"].split()[0])
    except (OSError, KeyError, ValueError):
        return None
    return 100.0 * available / total if total else None


def process_prefix() -> str:
    """The owner prefix every thread of this process shares."""
    return f"{socket.gethostname()}:{os.getpid()}:"


def _insert_followup(c, f: dict[str, Any], parent: dict[str, Any]) -> bool:
    """Insert a follow-up job. It inherits the settled job's pins (bundle, config hash,
    and its input when it gives none) only within the same task version: a retry of
    a rollout slot must be scored against the same bundle, but the first job of the
    next version (e.g. a world mutation) must not be pinned to the old one. An input
    the follow-up states, even an empty one, is used as given."""
    same = f["task_id"] == parent["task_id"] and f["version"] == parent["version"]
    parent_input = json.loads(parent["input"]) if isinstance(parent["input"], str) else parent["input"]
    inherit = lambda key, own: own if own or not same else parent[key]  # noqa: E731
    return insert_job(
        c, id=f["id"], task_id=f["task_id"], version=f["version"], type=JobType(f["type"]),
        input=f["input"] if "input" in f else (parent_input if same else {}),
        slot=f.get("slot", 0), retry=f.get("retry", 0),
        bundle_sha256=inherit("bundle_sha256", f.get("bundle_sha256")),
        config_hash=inherit("config_hash", f.get("config_hash")),
        delay_s=f.get("delay_sec", 0.0), priority=f.get("priority") or None,
    )
