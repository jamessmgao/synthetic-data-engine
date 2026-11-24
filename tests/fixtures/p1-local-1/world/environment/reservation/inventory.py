import json
from pathlib import Path

from .storage import connect, transaction

SCHEMA = """
CREATE TABLE IF NOT EXISTS stock (
    sku TEXT PRIMARY KEY,
    quantity INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS ledger (
    movement_id INTEGER PRIMARY KEY AUTOINCREMENT,
    reference TEXT NOT NULL,
    sku TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    remaining INTEGER NOT NULL
);
"""


class InventoryStore:
    def __init__(self, path: Path):
        self.db = connect(path)
        self.db.executescript(SCHEMA)
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def put_stock(self, sku: str, quantity: int) -> None:
        if quantity < 0:
            raise ValueError("stock cannot be negative")
        with transaction(self.db):
            self.db.execute(
                "INSERT INTO stock(sku, quantity) VALUES (?, ?) "
                "ON CONFLICT(sku) DO UPDATE SET quantity = excluded.quantity",
                (sku, quantity),
            )

    def reserve(self, reference: str, sku: str, quantity: int) -> dict:
        if quantity <= 0:
            raise ValueError("reservation quantity must be positive")
        with transaction(self.db):
            row = self.db.execute(
                "SELECT quantity FROM stock WHERE sku = ?", (sku,)
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown sku: {sku}")
            if row["quantity"] < quantity:
                return {"accepted": False, "reason": "insufficient_stock"}
            remaining = row["quantity"] - quantity
            self.db.execute(
                "UPDATE stock SET quantity = ? WHERE sku = ?", (remaining, sku)
            )
            cursor = self.db.execute(
                "INSERT INTO ledger(reference, sku, quantity, remaining) "
                "VALUES (?, ?, ?, ?)",
                (reference, sku, quantity, remaining),
            )
            return {
                "accepted": True,
                "movement_id": cursor.lastrowid,
                "remaining": remaining,
            }

    def status(self) -> dict:
        stock = [dict(row) for row in self.db.execute(
            "SELECT sku, quantity FROM stock ORDER BY sku"
        )]
        movements = [dict(row) for row in self.db.execute(
            "SELECT movement_id, reference, sku, quantity, remaining "
            "FROM ledger ORDER BY movement_id"
        )]
        return {"stock": stock, "movements": movements}

    def status_json(self) -> str:
        return json.dumps(self.status(), sort_keys=True)
