import json
import sqlite3
from pathlib import Path


class FulfillmentProvider:
    def __init__(self, path):
        self.path = Path(path)

    def _connect(self):
        return sqlite3.connect(self.path, timeout=5.0)

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                """CREATE TABLE reservations (
                       id INTEGER PRIMARY KEY AUTOINCREMENT,
                       order_id TEXT NOT NULL,
                       sku TEXT NOT NULL,
                       quantity INTEGER NOT NULL,
                       package_id TEXT NOT NULL,
                       status TEXT NOT NULL
                   )"""
            )
            conn.execute(
                "CREATE INDEX reservations_order_idx ON reservations(order_id)"
            )

    def reset(self):
        for suffix in ("", "-wal", "-shm", "-journal"):
            candidate = Path(str(self.path) + suffix)
            try:
                candidate.unlink()
            except FileNotFoundError:
                pass
        self.initialize()

    def reserve(self, order_id, sku, quantity):
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            count = conn.execute(
                "SELECT COUNT(*) FROM reservations WHERE order_id = ?",
                (order_id,),
            ).fetchone()[0]
            package_id = f"PKG-{order_id}-{count + 1}"
            conn.execute(
                """INSERT INTO reservations
                   (order_id, sku, quantity, package_id, status)
                   VALUES (?, ?, ?, ?, ?)""",
                (order_id, sku, quantity, package_id, "reserved"),
            )
            conn.commit()
            return {
                "package_id": package_id,
                "status": "reserved",
                "sku": sku,
                "quantity": quantity,
            }

    def packages(self, order_id):
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT package_id, sku, quantity, status
                   FROM reservations WHERE order_id = ? ORDER BY id""",
                (order_id,),
            ).fetchall()
        return [
            {
                "package_id": row[0],
                "sku": row[1],
                "quantity": row[2],
                "status": row[3],
            }
            for row in rows
        ]

    def first(self, order_id):
        packages = self.packages(order_id)
        return packages[0] if packages else None


class ResultCache:
    def __init__(self, path):
        self.path = Path(path)

    def _connect(self):
        return sqlite3.connect(self.path, timeout=5.0)

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                """CREATE TABLE responses (
                       order_id TEXT PRIMARY KEY,
                       status TEXT NOT NULL,
                       payload TEXT NOT NULL
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

    def put(self, order_id, status, result):
        payload = json.dumps(result, sort_keys=True, separators=(",", ":"))
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO responses(order_id, status, payload)
                   VALUES (?, ?, ?)
                   ON CONFLICT(order_id) DO UPDATE SET
                       status = excluded.status,
                       payload = excluded.payload""",
                (order_id, status, payload),
            )

    def get(self, order_id):
        with self._connect() as conn:
            row = conn.execute(
                "SELECT status, payload FROM responses WHERE order_id = ?",
                (order_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "order_id": order_id,
            "status": row[0],
            "result": json.loads(row[1]),
        }

    def remove(self, order_id):
        with self._connect() as conn:
            conn.execute("DELETE FROM responses WHERE order_id = ?", (order_id,))
