import json
import sqlite3
from pathlib import Path


class OrderStore:
    def __init__(self, path):
        self.path = Path(path)

    def _connect(self):
        return sqlite3.connect(self.path, timeout=5.0)

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                """CREATE TABLE orders (
                       order_id TEXT PRIMARY KEY,
                       sku TEXT NOT NULL,
                       quantity INTEGER NOT NULL,
                       status TEXT NOT NULL,
                       result TEXT
                   )"""
            )
            conn.execute(
                """CREATE TABLE queue (
                       order_id TEXT PRIMARY KEY,
                       sku TEXT NOT NULL,
                       quantity INTEGER NOT NULL,
                       deliveries INTEGER NOT NULL DEFAULT 0
                   )"""
            )

    def reset(self):
        for suffix in ("", "-wal", "-shm", "-journal"):
            candidate = Path(str(self.path) + suffix)
            try:
                candidate.unlink()
            except FileNotFoundError:
                pass
        self.initialize()

    def submit(self, order_id, sku, quantity):
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """INSERT INTO orders(order_id, sku, quantity, status)
                   VALUES (?, ?, ?, 'accepted')""",
                (order_id, sku, quantity),
            )
            conn.execute(
                """INSERT INTO queue(order_id, sku, quantity)
                   VALUES (?, ?, ?)""",
                (order_id, sku, quantity),
            )
            conn.commit()

    def next_queued(self):
        with self._connect() as conn:
            row = conn.execute(
                """SELECT order_id, sku, quantity
                   FROM queue ORDER BY rowid LIMIT 1"""
            ).fetchone()
        if row is None:
            return None
        return {"order_id": row[0], "sku": row[1], "quantity": row[2]}

    def mark_fulfilled(self, order_id, result):
        payload = json.dumps(result, sort_keys=True, separators=(",", ":"))
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            updated = conn.execute(
                """UPDATE orders
                   SET status = 'fulfilled', result = ?
                   WHERE order_id = ?""",
                (payload, order_id),
            ).rowcount
            if updated != 1:
                conn.rollback()
                raise KeyError(order_id)
            conn.execute("DELETE FROM queue WHERE order_id = ?", (order_id,))
            conn.commit()

    def finish_cached(self, order_id):
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM queue WHERE order_id = ?", (order_id,))
            conn.commit()

    def order(self, order_id):
        with self._connect() as conn:
            row = conn.execute(
                """SELECT order_id, sku, quantity, status, result
                   FROM orders WHERE order_id = ?""",
                (order_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "order_id": row[0],
            "sku": row[1],
            "quantity": row[2],
            "status": row[3],
            "result": json.loads(row[4]) if row[4] else None,
        }

    def waiting_count(self):
        with self._connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM queue").fetchone()[0]
