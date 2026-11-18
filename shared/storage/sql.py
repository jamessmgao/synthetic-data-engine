"""The `Store`: the two tables, transactions and the claim / heartbeat / fencing SQL.

One SQL text serves both backends (PRD §5.4). It sticks to the subset SQLite and
PostgreSQL share: `ON CONFLICT DO NOTHING`, `RETURNING`, `COUNT(*) FILTER`, partial
unique indexes. A dialect only rewrites placeholders and decides how a write
transaction takes the global write lock:

- SQLite: `BEGIN IMMEDIATE` (the database write lock).
- PostgreSQL: `BEGIN` + `pg_advisory_xact_lock`, which serializes state transitions
  the same way, so the Docker-slot count in a claim can't be raced.

Every state change is one such transaction, so compare-and-set updates are exact.
Readers (`sde status`) never take the lock.
"""

import json
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from shared.contracts.models import (
    DOCKER_JOB_TYPES,
    LEASE_TTL_S,
    MAX_JOB_ATTEMPTS,
    PRIORITY,
    JobStatus,
    JobType,
)

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS tasks (
        id               TEXT PRIMARY KEY,
        batch_id         TEXT NOT NULL,
        slot             TEXT NOT NULL DEFAULT '{}',
        status           TEXT NOT NULL,
        reason           TEXT,
        current_version  INTEGER NOT NULL DEFAULT 1 CHECK (current_version BETWEEN 1 AND 3),
        stage            TEXT NOT NULL,
        bundle_sha256    TEXT,
        name             TEXT,
        accepted_version INTEGER,
        created_at       TEXT NOT NULL,
        updated_at       TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS tasks_batch ON tasks (batch_id)",
    """CREATE TABLE IF NOT EXISTS jobs (
        id            TEXT PRIMARY KEY,
        task_id       TEXT NOT NULL REFERENCES tasks (id),
        version       INTEGER NOT NULL,
        type          TEXT NOT NULL,
        slot          INTEGER NOT NULL DEFAULT 0,
        retry         INTEGER NOT NULL DEFAULT 0,
        input         TEXT NOT NULL DEFAULT '{}',
        status        TEXT NOT NULL,
        due_at        DOUBLE PRECISION NOT NULL,
        priority      INTEGER NOT NULL,
        lease_owner   TEXT,
        lease_until   DOUBLE PRECISION,
        lease_token   INTEGER NOT NULL DEFAULT 0,
        attempts      INTEGER NOT NULL DEFAULT 0,
        isolated      INTEGER NOT NULL DEFAULT 0,
        bundle_sha256 TEXT,
        config_hash   TEXT,
        outcome       TEXT,
        result        TEXT,
        reward        DOUBLE PRECISION,
        error         TEXT,
        trace_uri     TEXT,
        trace_metrics TEXT,
        failure       TEXT,
        cost_usd      DOUBLE PRECISION,
        published_at  TEXT,
        handled_at    TEXT,
        created_at    TEXT NOT NULL,
        started_at    TEXT,
        finished_at   TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS jobs_claim ON jobs (type, status, priority, due_at)",
    "CREATE INDEX IF NOT EXISTS jobs_task ON jobs (task_id, version)",
    # At most one scored rollout per (version, slot): a retried or concurrently
    # settled slot can never be counted twice.
    """CREATE UNIQUE INDEX IF NOT EXISTS jobs_one_scored_slot ON jobs (task_id, version, slot)
        WHERE type = 'rollout' AND outcome IN ('VALID_PASS', 'VALID_FAIL')""",
    "CREATE INDEX IF NOT EXISTS jobs_unpublished ON jobs (published_at) WHERE published_at IS NULL",
    # Settled jobs the Job Service has not reacted to yet.
    """CREATE INDEX IF NOT EXISTS jobs_unhandled ON jobs (finished_at)
        WHERE handled_at IS NULL AND status IN ('DONE', 'DEAD')""",
]

_JSON_COLUMNS = ("slot", "input", "result", "trace_metrics", "failure")
_PG_LOCK_KEY = 0x6171  # "sde"


def utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


class Conn(Protocol):
    def execute(self, sql: str, params: tuple | list = ()) -> Any: ...


# --- dialects ---------------------------------------------------------------


class _SqliteConn:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._c = sqlite3.connect(path, timeout=30.0, isolation_level=None)
        self._c.row_factory = lambda cur, row: {d[0]: v for d, v in zip(cur.description, row)}
        self._c.execute("PRAGMA journal_mode=WAL")
        self._c.execute("PRAGMA busy_timeout=30000")
        self._c.execute("PRAGMA foreign_keys=ON")

    def execute(self, sql: str, params: tuple | list = ()) -> Any:
        return self._c.execute(sql, params)

    @contextmanager
    def tx(self) -> Iterator["_SqliteConn"]:
        self._c.execute("BEGIN IMMEDIATE")
        try:
            yield self
        except BaseException:
            self._c.execute("ROLLBACK")
            raise
        self._c.execute("COMMIT")


class _PostgresConn:
    def __init__(self, url: str):
        import psycopg
        from psycopg.rows import dict_row

        # prepare_threshold=None: no server-side prepared statements. A prepared
        # `SELECT * FROM jobs` breaks ("cached plan must not change result type")
        # once another process adds a column, e.g. during a rolling deploy.
        self._c = psycopg.connect(url, autocommit=True, row_factory=dict_row, prepare_threshold=None)

    def execute(self, sql: str, params: tuple | list = ()) -> Any:
        return self._c.execute(sql.replace("?", "%s"), params)

    @contextmanager
    def tx(self) -> Iterator["_PostgresConn"]:
        with self._c.transaction():
            self._c.execute("SELECT pg_advisory_xact_lock(%s)", (_PG_LOCK_KEY,))
            yield self


# --- row helpers (run inside the caller's transaction) ----------------------


def row_dict(row: dict[str, Any]) -> dict[str, Any]:
    d = dict(row)
    for col in _JSON_COLUMNS:
        if isinstance(d.get(col), str):
            d[col] = json.loads(d[col])
    return d


def job_id(task_id: str, version: int, kind: str, n: int = 0) -> str:
    """Deterministic job ids, e.g. `b1-t03-v2-repair1`. Also the Harbor job name.

    `create` starts v1; `revise` starts a later version (a revision); `repair<n>`
    (also type revise) fixes a version whose gates failed; `validate-r<n>` checks
    draft n of a version (0 = its create / revise, n = repair n)."""
    match kind:
        case "create" | "revise":
            return f"{task_id}-v{version}-{kind}"
        case "repair":
            return f"{task_id}-v{version}-repair{n}"
        case "validate":
            return f"{task_id}-v{version}-validate-r{n}"
        case _:
            raise ValueError(kind)


def rollout_job_id(task_id: str, version: int, slot: int, retry: int) -> str:
    return f"{task_id}-v{version}-rollout{slot}-r{retry}"


def insert_job(
    conn: Conn,
    *,
    id: str,
    task_id: str,
    version: int,
    type: JobType,
    input: dict[str, Any] | None = None,
    slot: int = 0,
    retry: int = 0,
    bundle_sha256: str | None = None,
    config_hash: str | None = None,
    delay_s: float = 0.0,
    priority: int | None = None,
) -> bool:
    """Create a job; inserting the same id again is a no-op. Returns True if new.

    Call `JobQueue.publish([id])` after the transaction commits so Redis sees it."""
    cur = conn.execute(
        """INSERT INTO jobs (id, task_id, version, type, slot, retry, input, status, due_at,
                             priority, bundle_sha256, config_hash, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT DO NOTHING""",
        (
            id, task_id, version, str(type), slot, retry, json.dumps(input or {}),
            str(JobStatus.READY), time.time() + delay_s,
            PRIORITY[JobType(type)] if priority is None else priority,
            bundle_sha256, config_hash, utcnow(),
        ),
    )
    return cur.rowcount == 1


def update_task(conn: Conn, task_id: str, **fields: Any) -> None:
    fields["updated_at"] = utcnow()
    cols = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(f"UPDATE tasks SET {cols} WHERE id = ?", (*fields.values(), task_id))


def claim(
    conn: Conn,
    owner: str,
    types: list[JobType],
    *,
    lease_s: float = LEASE_TTL_S,
    docker_slots: int | None = None,
    batch_id: str | None = None,
    only_id: str | None = None,
    process: str | None = None,
    host: str | None = None,
) -> dict[str, Any] | None:
    """Lease the next claimable job of the given types (or exactly `only_id`).

    Claimable means: READY, or LEASED with an expired lease (its worker died);
    claimed fewer than MAX_JOB_ATTEMPTS times; due; and its task not yet decided.
    Docker job types are only claimed while fewer than `docker_slots` live leases
    hold one (None = no cap). With `host` (the owner prefix of one machine), the cap
    counts only that machine's leases, so each runner gets its own slots; without
    it, the cap is global. Returns the leased row, `lease_token` included.

    Crash isolation: a job whose lease expired is a suspect, because its process
    died while running it (or while running a sibling). Given `process`, the owner
    prefix shared by one process's threads, a suspect is only claimed by a process
    holding no other live lease, and it is marked `isolated`. That process then
    claims nothing else until the suspect settles. A crash during an isolated run
    is charged to the suspect alone, so a poison job can't drag innocent siblings
    to MAX_JOB_ATTEMPTS with it.
    """
    now = time.time()
    t_marks = ",".join("?" * len(types))
    d_marks = ",".join("?" * len(DOCKER_JOB_TYPES))
    filters, extra = "", []
    if docker_slots is not None:
        on_host = " AND substr(lease_owner, 1, ?) = ?" if host else ""
        filters += f""" AND (type NOT IN ({d_marks}) OR (
                        SELECT COUNT(*) FROM jobs
                         WHERE type IN ({d_marks}) AND status = 'LEASED'
                           AND lease_until >= ?{on_host}) < ?)"""
        extra += [*map(str, DOCKER_JOB_TYPES), *map(str, DOCKER_JOB_TYPES), now,
                  *([len(host), host] if host else []), docker_slots]
    if batch_id:
        filters += " AND task_id IN (SELECT id FROM tasks WHERE batch_id = ?)"
        extra.append(batch_id)
    if only_id:
        filters += " AND id = ?"
        extra.append(only_id)
    if process:
        mine = "SELECT 1 FROM jobs WHERE status = 'LEASED' AND lease_until >= ? AND substr(lease_owner, 1, ?) = ?"
        filters += f""" AND NOT EXISTS ({mine} AND isolated = 1)
                   AND (status = 'READY' OR NOT EXISTS ({mine}))"""
        extra += [now, len(process), process, now, len(process), process]
    row = conn.execute(
        f"""UPDATE jobs SET lease_owner = ?, lease_until = ?,
                   lease_token = lease_token + 1, attempts = attempts + 1,
                   isolated = CASE WHEN status = 'LEASED' THEN 1 ELSE 0 END,
                   status = 'LEASED', started_at = COALESCE(started_at, ?)
             WHERE id = (
                SELECT id FROM jobs
                 WHERE type IN ({t_marks}) AND due_at <= ?
                   AND (status = 'READY' OR (status = 'LEASED' AND lease_until < ?))
                   AND attempts < ?
                   AND task_id NOT IN (SELECT id FROM tasks WHERE status IN ('ACCEPTED', 'REJECTED'))
                   {filters}
                 ORDER BY priority DESC, due_at
                 LIMIT 1)
         RETURNING *""",
        [owner, now + lease_s, utcnow(), *map(str, types), now, now, MAX_JOB_ATTEMPTS, *extra],
    ).fetchone()
    return row_dict(row) if row else None


def mark_handled(conn: Conn, job_id: str) -> bool:
    """Record that a settled job was reacted to (operator bookkeeping: `unhandled()`
    shows a backlog). False if it was already marked."""
    cur = conn.execute(
        "UPDATE jobs SET handled_at = ? WHERE id = ? AND handled_at IS NULL", (utcnow(), job_id)
    )
    return cur.rowcount == 1


def _split_generate_jobs(conn: Conn) -> None:
    """Migrate rows from before the create / revise split: no worker claims type
    `generate` any more. Repairs and revisions become `revise`, the rest `create`.
    Idempotent: a second run finds nothing to change."""
    conn.execute(  # patterns as parameters: `%` is psycopg's placeholder character
        """UPDATE jobs SET type = ?, priority = ?
            WHERE type = 'generate' AND (id LIKE ? OR input LIKE ? OR input LIKE ?)""",
        (str(JobType.REVISE), PRIORITY[JobType.REVISE],
         "%-repair%", '%"origin": "repair"%', '%"origin": "revision"%'),
    )
    conn.execute("UPDATE jobs SET type = ?, priority = ? WHERE type = 'generate'",
                 (str(JobType.CREATE), PRIORITY[JobType.CREATE]))


# --- the store ----------------------------------------------------------------


class SqlStore:
    """One database. Each thread gets its own connection."""

    def __init__(self, url: str):
        self.url = url
        self._local = threading.local()
        with self.tx() as conn:
            for stmt in SCHEMA:
                conn.execute(stmt)
            # Columns added after the first release (CREATE TABLE IF NOT EXISTS
            # leaves an existing table as it was).
            have = {d[0] for d in conn.execute("SELECT * FROM jobs LIMIT 0").description}
            if "isolated" not in have:
                conn.execute("ALTER TABLE jobs ADD COLUMN isolated INTEGER NOT NULL DEFAULT 0")
            _split_generate_jobs(conn)

    @property
    def conn(self) -> _SqliteConn | _PostgresConn:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            if self.url.startswith(("postgresql://", "postgres://")):
                conn = _PostgresConn(self.url)
            elif self.url.startswith("sqlite:///"):
                conn = _SqliteConn(Path(self.url.removeprefix("sqlite:///")))
            else:
                raise ValueError(f"unsupported DATABASE_URL: {self.url.split(':')[0]}://…")
            self._local.conn = conn
        return conn

    def tx(self):
        return self.conn.tx()

    # --- reads -----------------------------------------------------------------

    def task(self, task_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return row_dict(row) if row else None

    def batch_tasks(self, batch_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM tasks WHERE batch_id = ? ORDER BY id", (batch_id,)
        ).fetchall()
        return [row_dict(r) for r in rows]

    def task_jobs(self, task_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM jobs WHERE task_id = ? ORDER BY created_at, id", (task_id,)
        ).fetchall()
        return [row_dict(r) for r in rows]

    def job(self, id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM jobs WHERE id = ?", (id,)).fetchone()
        return row_dict(row) if row else None

    def batch_cost(self, batch_id: str) -> float:
        row = self.conn.execute(
            """SELECT COALESCE(SUM(j.cost_usd), 0) AS cost
                 FROM jobs j JOIN tasks t ON t.id = j.task_id WHERE t.batch_id = ?""",
            (batch_id,),
        ).fetchone()
        return float(row["cost"])

    def open_job_counts(self, batch_id: str | None = None) -> dict[str, int]:
        """Unfinished jobs: READY, LEASED with a live lease, LEASED with an expired one."""
        now = time.time()
        where, params = "", [now, now]
        if batch_id:
            where = "AND task_id IN (SELECT id FROM tasks WHERE batch_id = ?)"
            params.append(batch_id)
        row = self.conn.execute(
            f"""SELECT COUNT(*) FILTER (WHERE status = 'READY') AS ready,
                       COUNT(*) FILTER (WHERE status = 'LEASED' AND lease_until >= ?) AS leased,
                       COUNT(*) FILTER (WHERE status = 'LEASED' AND lease_until < ?) AS expired
                  FROM jobs WHERE status IN ('READY', 'LEASED') {where}""",
            params,
        ).fetchone()
        return {k: int(row[k] or 0) for k in ("ready", "leased", "expired")}

    def claimable_ids(self) -> list[dict[str, Any]]:
        """Every job a worker could claim now or later (for rebuilding a queue)."""
        rows = self.conn.execute(
            """SELECT id, type, due_at FROM jobs
                WHERE (status = 'READY' OR (status = 'LEASED' AND lease_until < ?))
                  AND attempts < ?
                  AND task_id NOT IN (SELECT id FROM tasks WHERE status IN ('ACCEPTED', 'REJECTED'))""",
            (time.time(), MAX_JOB_ATTEMPTS),
        ).fetchall()
        return list(rows)

    def unhandled(self, limit: int = 100) -> list[dict[str, Any]]:
        """Settled (DONE or DEAD) jobs the Job Service has not reacted to, oldest first."""
        rows = self.conn.execute(
            """SELECT * FROM jobs WHERE handled_at IS NULL AND status IN ('DONE', 'DEAD')
                ORDER BY finished_at, id LIMIT ?""",
            (limit,),
        ).fetchall()
        return [row_dict(r) for r in rows]
