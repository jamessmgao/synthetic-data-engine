#!/bin/sh
python3 - <<'PY'
from pathlib import Path

store_path = Path("/app/store.py")
service_path = Path("/app/service.py")
store_source = store_path.read_text()
method = '''    def settle_cached(self, order_id, result):
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

'''
marker = "    def order(self, order_id):\n"
if "def settle_cached(" not in store_source:
    if marker not in store_source:
        raise SystemExit(2)
    store_path.write_text(store_source.replace(marker, method + marker, 1))

service_source = service_path.read_text()
old = "store.finish_cached(order_id)"
new = 'store.settle_cached(order_id, cached["result"])'
if old in service_source:
    service_path.write_text(service_source.replace(old, new, 1))
PY
