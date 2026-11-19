import argparse
import json
import re
import sqlite3
import sys

from provider import FulfillmentProvider, ResultCache
from store import OrderStore


STATE_DIR = "/app/state"
ORDERS_DB = f"{STATE_DIR}/orders.db"
FULFILLMENT_DB = f"{STATE_DIR}/fulfillment.db"
CACHE_DB = f"{STATE_DIR}/responses.db"

store = OrderStore(ORDERS_DB)
provider = FulfillmentProvider(FULFILLMENT_DB)
cache = ResultCache(CACHE_DB)


def emit(value):
    print(json.dumps(value, sort_keys=True, separators=(",", ":")), flush=True)


def reset(_args):
    store.reset()
    provider.reset()
    cache.reset()
    emit({"state": "ready"})


def submit(args):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", args.order_id):
        raise ValueError("invalid order ID")
    if args.quantity < 1:
        raise ValueError("quantity must be positive")
    store.submit(args.order_id, args.sku, args.quantity)
    emit(
        {
            "order_id": args.order_id,
            "sku": args.sku,
            "quantity": args.quantity,
            "status": "accepted",
        }
    )


def worker(_args):
    processed = 0
    while True:
        queued = store.next_queued()
        if queued is None:
            break
        order_id = queued["order_id"]
        cached = cache.get(order_id)
        if cached is not None:
            store.finish_cached(order_id)
            emit(
                {
                    "order_id": order_id,
                    "phase": "complete",
                    "source": "cache",
                    "result": cached["result"],
                }
            )
            processed += 1
            continue
        result = provider.reserve(
            order_id, queued["sku"], queued["quantity"]
        )
        cache.put(order_id, "fulfilled", result)
        emit(
            {
                "order_id": order_id,
                "phase": "result_ready",
                "result": result,
            }
        )
        store.mark_fulfilled(order_id, result)
        emit(
            {
                "order_id": order_id,
                "phase": "settled",
                "result": result,
            }
        )
        processed += 1
    emit({"processed": processed})


def status(args):
    cached = cache.get(args.order_id)
    if cached is not None:
        emit(
            {
                "order_id": args.order_id,
                "status": cached["status"],
                "source": "cache",
                "result": cached["result"],
            }
        )
        return
    order = store.order(args.order_id)
    if order is None:
        raise KeyError(args.order_id)
    emit(
        {
            "order_id": order["order_id"],
            "status": order["status"],
            "source": "store",
            "result": order["result"],
        }
    )


def report(args):
    order = store.order(args.order_id)
    if order is None:
        raise KeyError(args.order_id)
    packages = provider.packages(args.order_id)
    cached = cache.get(args.order_id)
    emit(
        {
            "order": order,
            "queue_waiting": store.waiting_count(),
            "packages": packages,
            "package_count": len(packages),
            "cache": None if cached is None else cached,
        }
    )


def parser():
    result = argparse.ArgumentParser()
    subparsers = result.add_subparsers(dest="command", required=True)

    command = subparsers.add_parser("reset")
    command.set_defaults(func=reset)

    command = subparsers.add_parser("submit")
    command.add_argument("order_id")
    command.add_argument("sku")
    command.add_argument("quantity", type=int)
    command.set_defaults(func=submit)

    command = subparsers.add_parser("worker")
    command.set_defaults(func=worker)

    command = subparsers.add_parser("status")
    command.add_argument("order_id")
    command.set_defaults(func=status)

    command = subparsers.add_parser("report")
    command.add_argument("order_id")
    command.set_defaults(func=report)
    return result


def main():
    args = parser().parse_args()
    try:
        args.func(args)
    except KeyError as error:
        emit({"error": "not_found", "id": str(error.args[0])})
        return 1
    except (ValueError, sqlite3.Error) as error:
        emit({"error": str(error)})
        return 1
    return 0



if __name__ == "__main__":
    sys.exit(main())
