#!/bin/bash
set -euo pipefail

python3 - <<'PY'
from pathlib import Path

path = Path("/app/reservation/inventory.py")
source = path.read_text()
old = '''        with transaction(self.db):
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
'''
new = '''        with transaction(self.db):
            self.db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS ledger_reference "
                "ON ledger(reference)"
            )
            cursor = self.db.execute(
                "INSERT OR IGNORE INTO ledger(reference, sku, quantity, remaining) "
                "VALUES (?, ?, ?, ?)",
                (reference, sku, quantity, 0),
            )
            if cursor.rowcount == 0:
                previous = self.db.execute(
                    "SELECT movement_id, remaining FROM ledger "
                    "WHERE reference = ?", (reference,)
                ).fetchone()
                return {
                    "accepted": True,
                    "movement_id": previous["movement_id"],
                    "remaining": previous["remaining"],
                }
            row = self.db.execute(
                "SELECT quantity FROM stock WHERE sku = ?", (sku,)
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown sku: {sku}")
            if row["quantity"] < quantity:
                self.db.execute(
                    "DELETE FROM ledger WHERE movement_id = ?",
                    (cursor.lastrowid,),
                )
                return {"accepted": False, "reason": "insufficient_stock"}
            remaining = row["quantity"] - quantity
            self.db.execute(
                "UPDATE stock SET quantity = ? WHERE sku = ?", (remaining, sku)
            )
            self.db.execute(
                "UPDATE ledger SET remaining = ? WHERE movement_id = ?",
                (remaining, cursor.lastrowid),
            )
            return {
                "accepted": True,
                "movement_id": cursor.lastrowid,
                "remaining": remaining,
            }
'''
if old in source:
    source = source.replace(old, new, 1)
elif new not in source:
    raise SystemExit("expected inventory implementation was not found")
path.write_text(source)
PY
