#!/bin/bash
set -euo pipefail

export PYTHONPATH=/app
STATE=/tmp/reservation-repro
rm -rf "$STATE"
mkdir -p "$STATE"

reservation() {
  python3 -m reservation.cli --data-dir "$STATE" "$@"
}

reservation init --sku widget --stock 14
reservation submit --request-id order-1 --sku widget --quantity 7

set +e
STOP_AFTER=inventory python3 -m reservation.cli --data-dir "$STATE" worker --id worker-1
crash_status=$?
set -e

if [[ "$crash_status" -ne 70 ]]; then
  echo "fault injection returned $crash_status instead of 70" >&2
  exit 2
fi

reservation tick --milliseconds 30001
python3 -m reservation.cli --data-dir "$STATE" worker --id worker-2
reservation submit --request-id order-2 --sku widget --quantity 7
python3 -m reservation.cli --data-dir "$STATE" worker --id worker-3
reservation inspect --json > /tmp/reservation-report.json

python3 - <<'PY'
import json
from pathlib import Path

report = json.loads(Path("/tmp/reservation-report.json").read_text())
stock = report["inventory"]["stock"][0]["quantity"]
by_request = {}
for movement in report["inventory"]["movements"]:
    by_request.setdefault(movement["reference"], []).append(movement)
orders = {order["request_id"]: order for order in report["orders"]["orders"]}
violations = []
for request_id in ("order-1", "order-2"):
    movements = by_request.get(request_id, [])
    state = orders[request_id]["state"]
    if len(movements) != 1:
        violations.append(f"{request_id} has {len(movements)} movements, expected 1")
    if state != "fulfilled":
        violations.append(f"{request_id} is {state}, expected fulfilled")
if stock != 0:
    violations.append(f"expected both reservations to consume all 14 units, found {stock}")
if violations:
    print("; ".join(violations))
    raise SystemExit(1)
print(f"invariants hold: stock={stock}, order-1 movements={len(by_request['order-1'])}, order-2 movements={len(by_request['order-2'])}")
PY
