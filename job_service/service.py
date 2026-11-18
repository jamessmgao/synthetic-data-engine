"""
The Job Service (docs/PRD.md §5.1, the diagram's "Job Service + Calibration").

Workers pull their own jobs and settle them: generate (task-builder), validate
(task-builder gates), rollout (executor). The Job Service is the only code that
changes a task's status, stage or version. It reads what the workers settled
and creates the next jobs.

It is level-triggered: `reconcile_task` looks at a task and all its job rows and
takes the next step the rows call for. It keeps no memory between calls, so a
crash, a restart or a second copy running at the same time needs no special
handling:

- each step runs in one write transaction, so two copies serialize;
- job ids are deterministic and inserts are `ON CONFLICT DO NOTHING`, so a
  repeated step creates nothing;
- the step is recomputed from the rows, so after a crash it is simply redone.

Per version n of a task:

    generate ─OK─► validate-r0 ─OK─► rollout1..5 ─► 1–3 passes: ACCEPTED (published)
       │              │FAILED              │          0 or 4–5, n < 3: v(n+1) revision
       │              ▼                    │          0 or 4–5, n = 3: REJECTED exhausted
       │           repair1 ─► validate-r1 ─► … (≤ MAX_REPAIRS_PER_VERSION, then REJECTED invalid)
       │                                   │INVALID / DEAD: retry the slot (≤ MAX_ROLLOUT_RETRIES,
       ▼FAILED: REJECTED invalid           ▼              then REJECTED infra_unresolved)
"""

import json
import os
import secrets
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from shared.storage import Backend
from shared.storage.sql import (
    insert_job,
    job_id,
    mark_handled,
    rollout_job_id,
    row_dict,
    update_task,
    utcnow,
)
from shared.contracts.models import (
    MAX_REPAIRS_PER_VERSION,
    MAX_ROLLOUT_RETRIES,
    MAX_VERSIONS,
    POLL_S,
    SCORED_OUTCOMES,
    TERMINAL_TASK_STATUSES,
    JobStatus,
    JobType,
    Outcome,
    RejectReason,
    Stage,
    TaskStatus,
)

from job_service import calibration, verdict
from job_service.calibration import config_hash, eval_config, in_band
from job_service.taxonomy import sample_specs

REPO_ROOT = Path(__file__).resolve().parent.parent
MAX_BATCH_COUNT = 50


class BatchConflict(Exception):
    """The batch id exists with a different task count."""


def new_batch_id() -> str:
    return f"b-{datetime.now(UTC):%Y%m%d}-{secrets.token_hex(3)}"


def task_ids_for(batch_id: str, count: int) -> list[str]:
    return [f"{batch_id}-t{i:02d}" for i in range(1, count + 1)]


def _settled(job: dict[str, Any]) -> bool:
    return job["status"] in (JobStatus.DONE, JobStatus.DEAD)


def decision_text(task: dict[str, Any], jobs: list[dict[str, Any]]) -> str:
    """One line a reviewer can check against the rollout table."""
    cfg = eval_config()
    low, high = cfg["band"]
    if task["status"] == TaskStatus.ACCEPTED:
        v = task["accepted_version"]
        passes = sum(
            j["type"] == JobType.ROLLOUT and j["version"] == v and j["outcome"] == Outcome.VALID_PASS for j in jobs
        )
        return f"ACCEPTED: v{v} passed {passes}/{cfg['n_runs']} valid rollouts (band {low}–{high})"
    if task["status"] == TaskStatus.REJECTED:
        return f"REJECTED ({task['reason']}) at v{task['current_version']}"
    return f"PENDING: v{task['current_version']} is {task['stage']}"


class JobService:
    def __init__(self, backend: Backend, accepted_dir: Path | None = None):
        self.store = backend.store
        self.queue = backend.queue
        self.artifacts = backend.artifacts
        self.accepted_dir = accepted_dir or Path(os.environ.get("SDE_ACCEPTED_DIR", REPO_ROOT / "output" / "accepted"))
        self.n_runs = eval_config()["n_runs"]

    # --- submission ------------------------------------------------------------

    def submit_batch(self, count: int, batch_id: str | None = None) -> tuple[str, list[str], bool]:
        """Create `count` tasks and their v1 generate jobs in one transaction.

        Idempotent on `batch_id`: ids and specs are deterministic, so repeating a
        submission inserts nothing. Returns (batch_id, task_ids, created).
        """
        if not 1 <= count <= MAX_BATCH_COUNT:
            raise ValueError(f"count must be 1–{MAX_BATCH_COUNT}")
        batch_id = batch_id or new_batch_id()
        task_ids = task_ids_for(batch_id, count)
        specs = sample_specs(batch_id, count)
        new_jobs = []
        with self.store.tx() as conn:
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
                    (task_id, batch_id, _dumps(spec), str(TaskStatus.CREATED), str(Stage.QUEUED), now, now),
                )
                new_jobs += self._insert(
                    conn, id=job_id(task_id, 1, "generate"), task_id=task_id, version=1,
                    type=JobType.GENERATE, input={"origin": "initial", "spec": spec},
                )
        self.queue.publish(new_jobs)
        return batch_id, task_ids, existing == 0

    # --- the loop ----------------------------------------------------------------

    def open_task_ids(self, batch_id: str | None = None) -> list[str]:
        sql = "SELECT id FROM tasks WHERE status NOT IN (?, ?)"
        params: list[Any] = [str(s) for s in TERMINAL_TASK_STATUSES]
        if batch_id:
            sql += " AND batch_id = ?"
            params.append(batch_id)
        return [r["id"] for r in self.store.conn.execute(sql + " ORDER BY id", params).fetchall()]

    def reconcile(self, batch_id: str | None = None) -> int:
        """One pass over every open task, then any accepted task not yet
        published (a crash between the ACCEPTED commit and the copy). Returns
        how many tasks took a step."""
        stepped = sum(self.reconcile_task(task_id) for task_id in self.open_task_ids(batch_id))
        sql, params = "SELECT * FROM tasks WHERE status = ?", [str(TaskStatus.ACCEPTED)]
        if batch_id:
            sql += " AND batch_id = ?"
            params.append(batch_id)
        for row in self.store.conn.execute(sql, params).fetchall():
            task = row_dict(row)
            if self.published_dir(task) is None:
                self.publish(task, self.store.task_jobs(task["id"]))
        return stepped

    def run(self, batch_id: str | None = None, until_done: bool = False,
            stop: threading.Event | None = None, poll_s: float = POLL_S) -> None:
        """Reconcile until stopped (or, with `until_done`, until every task is final)."""
        stop = stop or threading.Event()
        while not stop.is_set():
            stepped = self.reconcile(batch_id)
            if until_done and not self.open_task_ids(batch_id):
                return
            if not stepped:
                stop.wait(poll_s)

    def reconcile_task(self, task_id: str) -> bool:
        """Take the next step for one task, if its jobs call for one."""
        new_jobs: list[dict[str, Any]] = []
        with self.store.tx() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if row is None:
                return False
            task = row_dict(row)
            if task["status"] in TERMINAL_TASK_STATUSES:
                return False
            jobs = [row_dict(j) for j in conn.execute(
                "SELECT * FROM jobs WHERE task_id = ? ORDER BY created_at, id", (task_id,)
            ).fetchall()]
            stepped = _Step(self, conn, task, jobs, new_jobs).run()
            # Everything settled so far has now been acted on; `handled_at` lets
            # operators see a backlog (store.unhandled()), it doesn't drive steps.
            for j in jobs:
                if _settled(j) and not j.get("handled_at"):
                    mark_handled(conn, j["id"])
        # After commit: the queue must not see uncommitted jobs, and copying the
        # bundle must not hold the global write lock.
        self.queue.publish(new_jobs)
        if task["status"] == TaskStatus.ACCEPTED:
            self.publish(task, jobs)
        return stepped

    # --- helpers -------------------------------------------------------------------

    def _insert(self, conn, **job: Any) -> list[dict[str, Any]]:
        """Insert a job; returns it (for queue publishing) if it is new."""
        if not insert_job(conn, **job):
            return []
        row = conn.execute("SELECT id, type, due_at FROM jobs WHERE id = ?", (job["id"],)).fetchone()
        return [dict(row)]

    def failing_tests(self, rollout: dict[str, Any]) -> list[str]:
        """Failing pytest lines from a failed rollout's verifier output, if it's
        on this machine (the executor's trace dir: <trial>/verifier/test-stdout.txt)."""
        trial = (rollout.get("result") or {}).get("trial")
        if rollout["outcome"] != Outcome.VALID_FAIL or not trial:
            return []
        stdout = self.artifacts.trace_dir(rollout["id"]) / trial / "verifier" / "test-stdout.txt"
        try:
            return calibration.failing_tests(stdout.read_text(errors="replace"))
        except OSError:
            return []

    def _destinations(self, task: dict[str, Any]) -> list[Path]:
        """Where an accepted task goes: output/accepted/<name>/, or
        <name>-<task id>/ if another task already took the name."""
        name = task.get("name") or task["id"]
        return [self.accepted_dir / name, self.accepted_dir / f"{name}-{task['id']}"]

    @staticmethod
    def _owned_by(dest: Path, task: dict[str, Any]) -> bool:
        verdict_md = dest / "verdict.md"
        return verdict_md.exists() and verdict_md.read_text().startswith(f"# Verdict: {task['id']}\n")

    def published_dir(self, task: dict[str, Any]) -> Path | None:
        return next((d for d in self._destinations(task) if self._owned_by(d, task)), None)

    def publish(self, task: dict[str, Any], jobs: list[dict[str, Any]]) -> Path:
        """Copy the accepted bundle and its verdict.md to output/accepted/.

        Runs after the ACCEPTED commit and is idempotent; if the process dies
        before it finishes, the next `reconcile` pass publishes again.
        """
        if (done := self.published_dir(task)) is not None:
            return done
        mine, fallback = self._destinations(task)
        # Fall back only if *another* task holds the name. A directory appears
        # whole (copied to a temp dir, then renamed), so if a second copy of
        # this service published it a moment ago, it already carries our
        # verdict and publishing to `mine` again is a no-op.
        dest = fallback if mine.exists() and not self._owned_by(mine, task) else mine
        text = verdict.render(task, jobs, decision_text(task, jobs), self.n_runs)
        self.artifacts.publish(task["bundle_sha256"], dest, text)
        return dest


class _Step:
    """Works out and applies the next step for one task, inside one transaction."""

    def __init__(self, svc: JobService, conn, task: dict[str, Any], jobs: list[dict[str, Any]],
                 new_jobs: list[dict[str, Any]]):
        self.svc, self.conn, self.task, self.all_jobs, self.new_jobs = svc, conn, task, jobs, new_jobs
        self.n = task["current_version"]
        self.jobs = {j["id"]: j for j in jobs if j["version"] == self.n}
        self.changed = False

    def set(self, **fields: Any) -> None:
        diff = {k: v for k, v in fields.items() if self.task.get(k) != v}
        if diff:
            update_task(self.conn, self.task["id"], **{k: _plain(v) for k, v in diff.items()})
            self.task.update(diff)
            self.changed = True

    def insert(self, **job: Any) -> None:
        added = self.svc._insert(self.conn, task_id=self.task["id"], version=self.n, **job)
        self.new_jobs += added
        self.changed |= bool(added)

    def reject(self, stage: Stage, reason: RejectReason) -> bool:
        self.set(status=TaskStatus.REJECTED, stage=stage, reason=reason)
        # Work still queued or running for this task can't change its verdict:
        # cancel it. A worker holding one of these loses its fenced heartbeat /
        # settle and stops; nothing would ever claim or dead-letter them otherwise.
        now = utcnow()
        cur = self.conn.execute(
            """UPDATE jobs SET status = 'DEAD', finished_at = ?, lease_until = NULL, handled_at = ?,
                               error = ?
                WHERE task_id = ? AND status IN ('READY', 'LEASED')""",
            (now, now, f"cancelled: task REJECTED ({reason})", self.task["id"]),
        )
        self.changed |= cur.rowcount > 0
        return self.changed

    def run(self) -> bool:
        tid, n = self.task["id"], self.n

        # 1. The version's latest draft: its generate job, or the newest repair.
        k = 0
        while job_id(tid, n, "repair", k + 1) in self.jobs:
            k += 1
        draft = self.jobs.get(job_id(tid, n, "generate") if k == 0 else job_id(tid, n, "repair", k))
        if draft is None:
            return False
        if draft["status"] != JobStatus.READY and self.task["status"] == TaskStatus.CREATED:
            self.set(status=TaskStatus.CALIBRATING)
        if not _settled(draft):
            if draft["status"] == JobStatus.LEASED:  # still QUEUED until a worker holds it
                self.set(stage=Stage.GENERATING)
            return self.changed
        if draft["status"] == JobStatus.DEAD:
            return self.reject(Stage.UNRESOLVED, RejectReason.INFRA_UNRESOLVED)
        if draft["outcome"] != Outcome.OK:
            return self.reject(Stage.INVALID, RejectReason.INVALID)
        built = draft.get("result") or {}
        sha = draft["bundle_sha256"] or built["bundle_sha256"]
        bundle_dir = str(self.svc.artifacts.bundle_path(sha))
        self.set(bundle_sha256=sha, name=built.get("name") or self.task["name"])

        # 2. Build + validate that draft; repair it if a gate fails.
        validate = self.jobs.get(job_id(tid, n, "validate", k))
        if validate is None:
            self.insert(id=job_id(tid, n, "validate", k), type=JobType.VALIDATE, bundle_sha256=sha,
                        input={"bundle_sha256": sha, "bundle_dir": bundle_dir})
            self.set(stage=Stage.VALIDATING)
            return self.changed
        if not _settled(validate):
            self.set(stage=Stage.VALIDATING)
            return self.changed
        if validate["status"] == JobStatus.DEAD:
            return self.reject(Stage.UNRESOLVED, RejectReason.INFRA_UNRESOLVED)
        if validate["outcome"] != Outcome.OK:
            if k >= MAX_REPAIRS_PER_VERSION:
                return self.reject(Stage.INVALID, RejectReason.INVALID)
            feedback = (validate.get("result") or {}).get("feedback") or validate["error"] or "a gate failed"
            self.insert(id=job_id(tid, n, "repair", k + 1), type=JobType.GENERATE,
                        input={"origin": "repair", "bundle_sha256": sha, "feedback": feedback})
            self.set(stage=Stage.GENERATING)
            return self.changed

        # 3. Five valid rollouts. Invalid or dead attempts are retried, never scored.
        scored: dict[int, dict[str, Any]] = {}
        waiting = exhausted = False
        for slot in range(1, self.svc.n_runs + 1):
            tries = sorted((j for j in self.jobs.values() if j["type"] == JobType.ROLLOUT and j["slot"] == slot),
                           key=lambda j: j["retry"])
            hit = next((j for j in tries if j["outcome"] in SCORED_OUTCOMES), None)
            if hit:
                scored[slot] = hit
                continue
            last = tries[-1] if tries else None
            if last is not None and not _settled(last):
                waiting = True
            elif last is not None and last["retry"] >= MAX_ROLLOUT_RETRIES:
                exhausted = True
            else:
                retry = 0 if last is None else last["retry"] + 1
                # The executor usually creates the retry itself when it settles an
                # INVALID run; this covers DEAD jobs and is a no-op otherwise.
                self.insert(id=rollout_job_id(tid, n, slot, retry), type=JobType.ROLLOUT, slot=slot,
                            retry=retry, bundle_sha256=sha, config_hash=config_hash(),
                            input={"task_dir": bundle_dir})
                waiting = True
        if exhausted:
            return self.reject(Stage.UNRESOLVED, RejectReason.INFRA_UNRESOLVED)
        if waiting:
            self.set(stage=Stage.EVALUATING)
            return self.changed

        # 4. Calibration: accept, revise, or give up.
        passes = sum(j["outcome"] == Outcome.VALID_PASS for j in scored.values())
        if in_band(passes):
            # Published by reconcile_task once this commits.
            self.set(status=TaskStatus.ACCEPTED, stage=Stage.BAND_OK, accepted_version=n)
            return True
        if n >= MAX_VERSIONS:
            return self.reject(Stage.OUT_OF_BAND, RejectReason.EXHAUSTED)
        used = [(j.get("input") or {}).get("operator") for j in self.all_jobs if j["type"] == JobType.GENERATE]
        operator = calibration.choose_operator(passes, list(scored.values()), [op for op in used if op])
        runs = [{**r, "failing_tests": self.svc.failing_tests(r)} for r in scored.values()]
        self.n += 1
        self.insert(id=job_id(tid, self.n, "generate"), type=JobType.GENERATE, input={
            "origin": "revision", "bundle_sha256": sha, "operator": operator,
            "result": f"{passes} of {self.svc.n_runs} runs passed.",
            "evidence": calibration.evidence(runs),
        })
        self.set(current_version=self.n, stage=Stage.QUEUED, bundle_sha256=None)
        return True


def _plain(value: Any) -> Any:
    """Enum members go into SQL as their text value."""
    return str(value) if hasattr(value, "value") else value


def _dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True)
