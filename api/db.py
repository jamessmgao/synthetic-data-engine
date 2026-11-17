"""
SQLite store: the two tables from docs/PRD.md §3 (`tasks`, `jobs`).

Every state change is one `BEGIN IMMEDIATE` transaction, so two processes
writing the same file serialize instead of interleaving. WAL mode lets readers
(status, inspect, report) run while a writer holds the lock.
"""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id               TEXT PRIMARY KEY,
    batch_id         TEXT NOT NULL,
    slot             TEXT,
    status           TEXT NOT NULL,
    reason           TEXT,
    current_version  INTEGER NOT NULL DEFAULT 1 CHECK (current_version BETWEEN 1 AND 3),
    stage            TEXT NOT NULL,
    bundle_sha256    TEXT,
    name             TEXT,
    accepted_version INTEGER CHECK (accepted_version BETWEEN 1 AND 3),
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS tasks_batch ON tasks (batch_id);

CREATE TABLE IF NOT EXISTS jobs (
    id            TEXT PRIMARY KEY,
    task_id       TEXT NOT NULL REFERENCES tasks (id),
    version       INTEGER NOT NULL CHECK (version BETWEEN 1 AND 3),
    type          TEXT NOT NULL,
    slot          INTEGER,
    retry         INTEGER NOT NULL DEFAULT 0,
    input         TEXT NOT NULL DEFAULT '{}',
    status        TEXT NOT NULL,
    due_at        TEXT NOT NULL,
    priority      INTEGER NOT NULL,
    lease_owner   TEXT,
    lease_until   TEXT,
    lease_token   INTEGER NOT NULL DEFAULT 0,
    attempts      INTEGER NOT NULL DEFAULT 0,
    bundle_sha256 TEXT,
    config_hash   TEXT,
    outcome       TEXT,
    result        TEXT,
    reward        REAL,
    error         TEXT,
    trace_uri     TEXT,
    trace_metrics TEXT,
    failure       TEXT,
    cost_usd      REAL,
    published_at  TEXT,
    created_at    TEXT NOT NULL,
    started_at    TEXT,
    finished_at   TEXT
);
CREATE INDEX IF NOT EXISTS jobs_task ON jobs (task_id, version);
CREATE INDEX IF NOT EXISTS jobs_claim ON jobs (type, status, due_at);
-- At most one scored rollout per (version, slot): a retried or reclaimed slot
-- can never be counted twice.
CREATE UNIQUE INDEX IF NOT EXISTS jobs_one_scored_slot ON jobs (task_id, version, slot)
    WHERE type = 'rollout' AND outcome IN ('VALID_PASS', 'VALID_FAIL');
"""


def utcnow() -> str:
    """UTC timestamp as fixed-width ISO text, so string order is time order."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def parse_ts(ts: str) -> datetime:
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open a connection in autocommit mode; use `transaction()` for writes."""
    conn = sqlite3.connect(db_path, timeout=30, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(db_path: str | Path) -> None:
    """Create the database file and tables if they don't exist yet."""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """One write transaction. Takes the write lock up front (`BEGIN IMMEDIATE`)
    so a read-then-write inside it can't race another process."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")
