import json
import sqlite3
from pathlib import Path

from .storage import connect, transaction

SCHEMA = """
CREATE TABLE IF NOT EXISTS clock (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    now_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS orders (
    request_id TEXT PRIMARY KEY,
    sku TEXT NOT NULL,
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    state TEXT NOT NULL CHECK (state IN ('queued', 'running', 'fulfilled', 'rejected')),
    attempts INTEGER NOT NULL DEFAULT 0,
    lease_until_ms INTEGER,
    last_worker TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS orders_pending
    ON orders(request_id) WHERE state IN ('queued', 'running');
"""


class OrderStore:
    def __init__(self, path: Path):
        self.db = connect(path)
        self.db.executescript(SCHEMA)
        self.db.execute(
            "INSERT OR IGNORE INTO clock(id, now_ms) VALUES (1, 0)"
        )
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def now(self) -> int:
        row = self.db.execute("SELECT now_ms FROM clock WHERE id = 1").fetchone()
        return row["now_ms"]

    def advance(self, milliseconds: int) -> int:
        if milliseconds < 0:
            raise ValueError("time cannot go backward")
        with transaction(self.db):
            self.db.execute(
                "UPDATE clock SET now_ms = now_ms + ? WHERE id = 1",
                (milliseconds,),
            )
        return self.now()

    def submit(self, request_id: str, sku: str, quantity: int) -> dict:
        if not request_id or quantity <= 0:
            raise ValueError("request_id and positive quantity are required")
        with transaction(self.db):
            existing = self.db.execute(
                "SELECT request_id, sku, quantity, state, attempts "
                "FROM orders WHERE request_id = ?", (request_id,)
            ).fetchone()
            if existing is not None:
                if existing["sku"] != sku or existing["quantity"] != quantity:
                    raise ValueError("request_id conflicts with an existing order")
                return dict(existing)
            self.db.execute(
                "INSERT INTO orders(request_id, sku, quantity, state) "
                "VALUES (?, ?, ?, 'queued')",
                (request_id, sku, quantity),
            )
        return {
            "request_id": request_id,
            "sku": sku,
            "quantity": quantity,
            "state": "queued",
            "attempts": 0,
        }

    def claim(self, worker: str, lease_ms: int) -> dict | None:
        now = self.now()
        with transaction(self.db):
            row = self.db.execute(
                "SELECT * FROM orders "
                "WHERE state = 'queued' "
                "   OR (state = 'running' AND lease_until_ms <= ?) "
                "ORDER BY attempts, request_id LIMIT 1", (now,)
            ).fetchone()
            if row is None:
                return None
            self.db.execute(
                "UPDATE orders SET state = 'running', attempts = attempts + 1, "
                "lease_until_ms = ?, last_worker = ? WHERE request_id = ?",
                (now + lease_ms, worker, row["request_id"]),
            )
            updated = self.db.execute(
                "SELECT * FROM orders WHERE request_id = ?",
                (row["request_id"],),
            ).fetchone()
            return dict(updated)

    def finish(self, request_id: str, fulfilled: bool) -> None:
        state = "fulfilled" if fulfilled else "rejected"
        with transaction(self.db):
            count = self.db.execute(
                "UPDATE orders SET state = ?, lease_until_ms = NULL "
                "WHERE request_id = ? AND state = 'running'",
                (state, request_id),
            ).rowcount
            if count != 1:
                raise sqlite3.OperationalError("order is not leased by this worker")

    def status(self) -> dict:
        now = self.now()
        rows = [dict(row) for row in self.db.execute(
            "SELECT request_id, sku, quantity, state, attempts, "
            "lease_until_ms, last_worker FROM orders ORDER BY request_id"
        )]
        return {"now_ms": now, "orders": rows}

    def status_json(self) -> str:
        return json.dumps(self.status(), sort_keys=True)
