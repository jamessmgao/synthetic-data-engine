import argparse
import json
from pathlib import Path

from .inventory import InventoryStore
from .orders import OrderStore
from .worker import Worker

DEFAULT_DATA_DIR = "/tmp/reservation-data"


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="reservation")
    root.add_argument("--data-dir", default=DEFAULT_DATA_DIR, type=Path)
    sub = root.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init")
    init.add_argument("--sku", required=True)
    init.add_argument("--stock", required=True, type=int)

    submit = sub.add_parser("submit")
    submit.add_argument("--request-id", required=True)
    submit.add_argument("--sku", required=True)
    submit.add_argument("--quantity", required=True, type=int)

    tick = sub.add_parser("tick")
    tick.add_argument("--milliseconds", required=True, type=int)

    worker = sub.add_parser("worker")
    worker.add_argument("--id", default="worker-1")

    inspect = sub.add_parser("inspect")
    inspect.add_argument("--json", action="store_true")
    return root


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    orders_path = args.data_dir / "orders.db"
    inventory_path = args.data_dir / "inventory.db"
    orders = OrderStore(orders_path)
    inventory = InventoryStore(inventory_path)
    try:
        if args.command == "init":
            inventory.put_stock(args.sku, args.stock)
            print(f"initialized {args.sku} with {args.stock} units")
        elif args.command == "submit":
            print(json.dumps(orders.submit(
                args.request_id, args.sku, args.quantity
            ), sort_keys=True))
        elif args.command == "tick":
            print(json.dumps({"now_ms": orders.advance(args.milliseconds)}, sort_keys=True))
        elif args.command == "worker":
            result = Worker(orders, inventory).run_once(args.id)
            if result is None:
                print(json.dumps({"claimed": False}, sort_keys=True))
            else:
                print(json.dumps(result, sort_keys=True))
        elif args.command == "inspect":
            report = {
                "orders": orders.status(),
                "inventory": inventory.status(),
            }
            if args.json:
                print(json.dumps(report, sort_keys=True))
            else:
                print(json.dumps(report, indent=2, sort_keys=True))
    finally:
        orders.close()
        inventory.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
