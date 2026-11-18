import os
from pathlib import Path

from .inventory import InventoryStore
from .orders import OrderStore

DEFAULT_LEASE_MS = 30000


class Worker:
    def __init__(self, orders: OrderStore, inventory: InventoryStore):
        self.orders = orders
        self.inventory = inventory

    def run_once(self, worker_id: str = "worker-1") -> dict | None:
        order = self.orders.claim(worker_id, DEFAULT_LEASE_MS)
        if order is None:
            return None
        result = self.inventory.reserve(
            order["request_id"], order["sku"], order["quantity"]
        )
        if os.environ.get("STOP_AFTER") == "inventory":
            os._exit(70)
        self.orders.finish(order["request_id"], result["accepted"])
        return {**order, "inventory": result}
