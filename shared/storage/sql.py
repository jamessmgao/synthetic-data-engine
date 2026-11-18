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
import logging
import os
import re
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from shared.storage.budget import claim_filter as claim_budget_filter
from shared.contracts.models import (
    AGENT_JOB_TYPES,
    DOCKER_JOB_TYPES,
    LEASE_TTL_S,
    MAX_JOB_ATTEMPTS,
    PRIORITY,
    JobStatus,
    JobType,
    needs_docker,
)

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS tasks (
        id               TEXT PRIMARY KEY,
        slot             TEXT NOT NULL DEFAULT '{}',
        status           TEXT NOT NULL,
        reason           TEXT,
        current_version  INTEGER NOT NULL DEFAULT 1 CHECK (current_version >= 1),
        stage            TEXT NOT NULL,
        bundle_sha256    TEXT,
        name             TEXT,
        accepted_version INTEGER,
        batch_id         TEXT,
        created_at       TEXT NOT NULL,
        updated_at       TEXT NOT NULL
    )""",
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
        needs_docker  INTEGER NOT NULL DEFAULT 0,
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
    # A batch's own settings (POST /v1/batches); its tasks carry its id in tasks.batch_id.
    """CREATE TABLE IF NOT EXISTS batches (
        id          TEXT PRIMARY KEY,
        budget_usd  DOUBLE PRECISION,
        created_at  TEXT NOT NULL
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


def _pg_sql(sql: str) -> str:
    # psycopg's placeholder is %s, so a literal % (LIKE 'x%') must become %%.
    return sql.replace("%", "%%").replace("?", "%s")


class _Result:
    """A statement's result, read in full before its pooled connection goes back."""

    def __init__(self, cur: Any):
        self.rowcount = cur.rowcount
        self.description = cur.description
        self._rows = cur.fetchall() if cur.description else []

    def fetchone(self) -> dict[str, Any] | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[dict[str, Any]]:
        return list(self._rows)


class _PostgresTx:
    """One pooled connection, held for one transaction."""

    def __init__(self, c: Any):
        self._c = c

    def execute(self, sql: str, params: tuple | list = ()) -> Any:
        return self._c.execute(_pg_sql(sql), params)


# One bounded pool per process and database: every thread (worker loops, heartbeats,
# the reconciler, API requests) borrows a connection for one statement or one
# transaction, so a process never holds more than SDE_DB_POOL_MAX connections. With
# a connection per thread, a few busy runners exhausted a small RDS instance.
_POOLS: dict[str, Any] = {}
_POOLS_LOCK = threading.Lock()
POOL_MAX = int(os.environ.get("SDE_DB_POOL_MAX", "4"))
POOL_TIMEOUT_S = float(os.environ.get("SDE_DB_POOL_TIMEOUT_S", "60"))


POOL_WARN_S = float(os.environ.get("SDE_DB_POOL_WARN_S", "2"))
# How long a suspect (expired lease) waits for an idle process before any may take it.
SUSPECT_MAX_WAIT_S = float(os.environ.get("SDE_SUSPECT_MAX_WAIT_S", "600"))
log = logging.getLogger("store")


def _pool(url: str, role: str = "main") -> Any:
    """The process's pool for `role`: "main" (POOL_MAX connections) for everything, and
    "heartbeat" (1 connection) for lease heartbeats only, so a pool busy with long
    transactions can never make a lease lapse under a running job."""
    with _POOLS_LOCK:
        pool = _POOLS.get((url, role))
        if pool is None:
            from psycopg.rows import dict_row
            from psycopg_pool import ConnectionPool

            # prepare_threshold=None: no server-side prepared statements. A prepared
            # `SELECT * FROM jobs` breaks ("cached plan must not change result type")
            # once another process adds a column, e.g. during a rolling deploy.
            pool = ConnectionPool(
                url, min_size=1, max_size=POOL_MAX if role == "main" else 1, timeout=POOL_TIMEOUT_S,
                open=True, name=f"sde-{os.getpid()}-{role}",
                kwargs={"autocommit": True, "row_factory": dict_row, "prepare_threshold": None,
                        "application_name": "sde-pipeline" if role == "main" else f"sde-pipeline-{role}"},
            )
            _POOLS[(url, role)] = pool
        return pool


@contextmanager
def _borrow(pool: Any) -> Iterator[Any]:
    """A pooled connection; a long wait for one is logged (pool contention)."""
    start = time.monotonic()
    with pool.connection() as c:
        waited = time.monotonic() - start
        if waited >= POOL_WARN_S:
            stats = pool.get_stats()
            log.warning("waited %.1fs for a database connection (pool %s: size %s, waiting %s)",
                        waited, pool.name, stats.get("pool_size"), stats.get("requests_waiting"))
        yield c


class _PostgresConn:
    """The Postgres side of the store, over one of the process's connection pools."""

    def __init__(self, url: str, role: str = "main"):
        self._pool = _pool(url, role)

    def execute(self, sql: str, params: tuple | list = ()) -> Any:
        with _borrow(self._pool) as c:
            return _Result(c.execute(_pg_sql(sql), params))

    @contextmanager
    def tx(self) -> Iterator[_PostgresTx]:
        with _borrow(self._pool) as c, c.transaction():
            c.execute("SELECT pg_advisory_xact_lock(%s)", (_PG_LOCK_KEY,))
            yield _PostgresTx(c)


# --- row helpers (run inside the caller's transaction) ----------------------


def row_dict(row: dict[str, Any]) -> dict[str, Any]:
    d = dict(row)
    for col in _JSON_COLUMNS:
        if isinstance(d.get(col), str):
            d[col] = json.loads(d[col])
    return d


def job_id(task_id: str, version: int, kind: str, n: int = 0) -> str:
    """Deterministic job ids, e.g. `t-20261118-1a2b3c4d-v2-repair1`. Also the Harbor job name.

    `create` starts v1; `revise` starts a later version (a revision); `repair<n>`
    (also type revise) fixes a version whose gates failed; `validate-r<n>` checks
    draft n of a version (0 = its create / revise, n = repair n). Agent mode adds
    `world` (builds or mutates the version's world), `world-repair<n>` (type world,
    after a failed check) and `world-check-r<n>` (checks world draft n)."""
    match kind:
        case "create" | "revise" | "world":
            return f"{task_id}-v{version}-{kind}"
        case "world-repair":
            return f"{task_id}-v{version}-world-repair{n}"
        case "world-check":
            return f"{task_id}-v{version}-world-check-r{n}"
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
                             priority, bundle_sha256, config_hash, needs_docker, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT DO NOTHING""",
        (
            id, task_id, version, str(type), slot, retry, json.dumps(input or {}),
            str(JobStatus.READY), time.time() + delay_s,
            PRIORITY[JobType(type)] if priority is None else priority,
            bundle_sha256, config_hash, int(needs_docker(str(type), input)), utcnow(),
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
    agent_slots: int | None = None,
    only_id: str | None = None,
    process: str | None = None,
    host: str | None = None,
) -> dict[str, Any] | None:
    """Lease the next claimable job of the given types (or exactly `only_id`).

    Claimable means: READY, or LEASED with an expired lease (its worker died);
    claimed fewer than MAX_JOB_ATTEMPTS times; due; and its task not yet decided.
    A job that needs Docker (jobs.needs_docker) is only claimed while fewer than
    `docker_slots` live leases hold a slot (None = no cap), and an agent job (world,
    create / revise from a world) only while fewer than `agent_slots` agent jobs do.
    With `host` (the owner prefix of one machine) the caps count only that machine's
    leases, so each runner gets its own slots; without it they are global. Returns
    the leased row, `lease_token` included.

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
    a_marks = ",".join("?" * len(AGENT_JOB_TYPES))
    filters, extra = "", []
    on_host = " AND substr(lease_owner, 1, ?) = ?" if host else ""
    host_args = [len(host), host] if host else []
    held = f"SELECT COUNT(*) FROM jobs WHERE status = 'LEASED' AND lease_until >= ? AND needs_docker = 1{on_host}"
    if docker_slots is not None:
        filters += f" AND (needs_docker = 0 OR ({held}) < ?)"
        extra += [now, *host_args, docker_slots]
    if agent_slots is not None:
        filters += f" AND (needs_docker = 0 OR type NOT IN ({a_marks}) OR ({held} AND type IN ({a_marks})) < ?)"
        extra += [*map(str, AGENT_JOB_TYPES), now, *host_args, *map(str, AGENT_JOB_TYPES), agent_slots]
    budget, budget_params = claim_budget_filter("j")  # SDE_BUDGET_USD and the batch's cap
    filters += budget
    extra += budget_params
    if only_id:
        filters += " AND id = ?"
        extra.append(only_id)
    if process:
        # A suspect (an expired lease) goes to a process holding no other lease, so a
        # crash during its run is charged to it alone. But a busy process (an executor
        # always holds a rollout) never qualifies, so after SUSPECT_MAX_WAIT_S any
        # process may take it: still isolated, and MAX_JOB_ATTEMPTS still dead-letters
        # a real poison job. Without this an abandoned job waited for hours.
        mine = "SELECT 1 FROM jobs WHERE status = 'LEASED' AND lease_until >= ? AND substr(lease_owner, 1, ?) = ?"
        filters += f""" AND NOT EXISTS ({mine} AND isolated = 1)
                   AND (j.status = 'READY' OR NOT EXISTS ({mine}) OR j.lease_until < ?)"""
        extra += [now, len(process), process, now, len(process), process, now - SUSPECT_MAX_WAIT_S]
    row = conn.execute(
        f"""UPDATE jobs SET lease_owner = ?, lease_until = ?,
                   lease_token = lease_token + 1, attempts = attempts + 1,
                   isolated = CASE WHEN status = 'LEASED' THEN 1 ELSE 0 END,
                   status = 'LEASED', started_at = COALESCE(started_at, ?)
             WHERE id = (
                SELECT j.id FROM jobs j
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


def docker_load(conn: Conn, host: str | None) -> dict[str, int]:
    """Live leases holding a Docker slot (and of those, agent jobs), on `host` or overall."""
    now = time.time()
    on_host = " AND substr(lease_owner, 1, ?) = ?" if host else ""
    marks = ",".join("?" * len(AGENT_JOB_TYPES))
    row = conn.execute(
        f"""SELECT COUNT(*) AS docker, COUNT(*) FILTER (WHERE type IN ({marks})) AS agent
              FROM jobs WHERE status = 'LEASED' AND lease_until >= ? AND needs_docker = 1{on_host}""",
        [*map(str, AGENT_JOB_TYPES), now, *([len(host), host] if host else [])],
    ).fetchone()
    return {"docker": int(row["docker"]), "agent": int(row["agent"])}


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


def _sqlite_rebuild_old_tasks(path: Path) -> None:
    """Databases created when tasks had at most 3 versions carry
    CHECK (current_version BETWEEN 1 AND 3); the cap is now MAX_VERSIONS, enforced
    in code. The first batch release also made `batch_id` NOT NULL; single tasks
    now leave it null. SQLite can't alter a constraint, so rebuild `tasks` the documented
    way (new table, copy, drop, rename) with foreign keys off: `jobs` keeps
    referencing `tasks`, so no reference is rewritten."""
    if not path.exists():
        return
    c = sqlite3.connect(path, timeout=30, isolation_level=None)
    try:
        row = c.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'tasks'").fetchone()
        if row is None or not ("BETWEEN 1 AND 3" in row[0] or re.search(r"batch_id\s+TEXT\s+NOT NULL", row[0])):
            return
        c.execute("PRAGMA foreign_keys=OFF")
        c.execute("BEGIN IMMEDIATE")
        cols = ", ".join(d[0] for d in c.execute("SELECT * FROM tasks LIMIT 0").description)
        c.execute(SCHEMA[0].replace("CREATE TABLE IF NOT EXISTS tasks", "CREATE TABLE tasks_new"))
        c.execute(f"INSERT INTO tasks_new ({cols}) SELECT {cols} FROM tasks")
        c.execute("DROP TABLE tasks")
        c.execute("ALTER TABLE tasks_new RENAME TO tasks")
        c.execute("COMMIT")
    finally:
        c.close()


# --- the store ----------------------------------------------------------------


class SqlStore:
    """One database. Each thread gets its own connection."""

    def __init__(self, url: str):
        self.url = url
        self._local = threading.local()
        if url.startswith("sqlite:///"):
            _sqlite_rebuild_old_tasks(Path(url.removeprefix("sqlite:///")))
        with self.tx() as conn:
            for stmt in SCHEMA:
                conn.execute(stmt)
            # Columns added after the first release (CREATE TABLE IF NOT EXISTS
            # leaves an existing table as it was).
            have = {d[0] for d in conn.execute("SELECT * FROM jobs LIMIT 0").description}
            if "isolated" not in have:
                conn.execute("ALTER TABLE jobs ADD COLUMN isolated INTEGER NOT NULL DEFAULT 0")
            if "needs_docker" not in have:
                conn.execute("ALTER TABLE jobs ADD COLUMN needs_docker INTEGER NOT NULL DEFAULT 0")
                marks = ",".join("?" * len(DOCKER_JOB_TYPES))
                conn.execute(f"UPDATE jobs SET needs_docker = 1 WHERE type IN ({marks})",
                             [str(t) for t in DOCKER_JOB_TYPES])
            _split_generate_jobs(conn)
            # A batch is a label on its tasks (POST /v1/batches); single tasks leave it null.
            task_cols = {d[0] for d in conn.execute("SELECT * FROM tasks LIMIT 0").description}
            if "batch_id" not in task_cols:
                conn.execute("ALTER TABLE tasks ADD COLUMN batch_id TEXT")
            conn.execute("CREATE INDEX IF NOT EXISTS tasks_batch ON tasks (batch_id)")
            if self.url.startswith(("postgresql://", "postgres://")):
                conn.execute("ALTER TABLE tasks ALTER COLUMN batch_id DROP NOT NULL")  # no-op when nullable
                # Lift the old 3-version cap once. Postgres names an inline column
                # check <table>_<column>_check; only an old definition is replaced,
                # so later starts (and runners booting together) change nothing.
                row = conn.execute(
                    "SELECT pg_get_constraintdef(oid) AS def FROM pg_constraint"
                    " WHERE conname = 'tasks_current_version_check'").fetchone()
                if row is not None and "3" in row["def"]:
                    conn.execute("ALTER TABLE tasks DROP CONSTRAINT tasks_current_version_check")
                    conn.execute("ALTER TABLE tasks ADD CONSTRAINT tasks_current_version_check"
                                 " CHECK (current_version >= 1)")

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

    @property
    def heartbeat_conn(self) -> _SqliteConn | _PostgresConn:
        """Postgres: a dedicated 1-connection pool (created on first use, so only
        processes that heartbeat hold it). SQLite: the thread's own connection."""
        if self.url.startswith(("postgresql://", "postgres://")):
            conn = getattr(self._local, "hb", None)
            if conn is None:
                conn = self._local.hb = _PostgresConn(self.url, "heartbeat")
            return conn
        return self.conn

    # --- reads -----------------------------------------------------------------

    def task(self, task_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return row_dict(row) if row else None

    def tasks(self, ids: list[str] | None = None, status: str | None = None,
              since: str | None = None, batch_id: str | None = None) -> list[dict[str, Any]]:
        """Tasks in creation order, optionally narrowed to ids, a status, a batch, or those created at/after `since`."""
        where, params = [], []
        if batch_id is not None:
            where.append("batch_id = ?")
            params.append(batch_id)
        if ids is not None:
            if not ids:
                return []
            where.append(f"id IN ({','.join('?' * len(ids))})")
            params += ids
        if status:
            where.append("status = ?")
            params.append(status)
        if since:
            where.append("created_at >= ?")
            params.append(since)
        sql = "SELECT * FROM tasks" + (" WHERE " + " AND ".join(where) if where else "")
        return [row_dict(r) for r in self.conn.execute(sql + " ORDER BY created_at, id", params).fetchall()]

    def batches(self) -> list[dict[str, Any]]:
        """Every batch with its task count per outcome, newest first."""
        rows = self.conn.execute(
            """SELECT batch_id, COUNT(*) AS tasks,
                      COUNT(*) FILTER (WHERE status = 'ACCEPTED') AS accepted,
                      COUNT(*) FILTER (WHERE status = 'REJECTED') AS rejected,
                      MIN(created_at) AS created_at
                 FROM tasks WHERE batch_id IS NOT NULL
                GROUP BY batch_id ORDER BY MIN(created_at) DESC, batch_id DESC"""
        ).fetchall()
        out = []
        for r in rows:
            counts = {k: int(r[k]) for k in ("tasks", "accepted", "rejected")}
            out.append({"batch_id": r["batch_id"], **counts,
                        "in_progress": counts["tasks"] - counts["accepted"] - counts["rejected"],
                        "created_at": r["created_at"]})
        return out

    def task_jobs(self, task_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM jobs WHERE task_id = ? ORDER BY created_at, id", (task_id,)
        ).fetchall()
        return [row_dict(r) for r in rows]

    def job(self, id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM jobs WHERE id = ?", (id,)).fetchone()
        return row_dict(row) if row else None

    def open_job_counts(self) -> dict[str, int]:
        """Unfinished jobs: READY, LEASED with a live lease, LEASED with an expired one."""
        now = time.time()
        where, params = "", [now, now]
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
