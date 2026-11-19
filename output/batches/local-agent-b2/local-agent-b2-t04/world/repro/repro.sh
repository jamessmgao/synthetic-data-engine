#!/bin/sh
exec python3 - <<'PY'
import json
import sqlite3
import subprocess
import sys

SERVICE = "/app/service.py"
ORDER_ID = "ORDER-42"


def run(arguments):
    return subprocess.run(
        ["python3", SERVICE, *arguments],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def one_json(arguments):
    process = run(arguments)
    if process.returncode != 0:
        print(json.dumps({
            "harness_error": "command_failed",
            "arguments": arguments,
            "code": process.returncode,
            "stderr": process.stderr.strip(),
        }))
        raise SystemExit(2)
    try:
        return json.loads(process.stdout)
    except json.JSONDecodeError as error:
        print(json.dumps({"harness_error": "invalid_json", "detail": str(error)}))
        raise SystemExit(2)


one_json(["reset"])
submitted = one_json(["submit", ORDER_ID, "widget", "3"])
if submitted.get("status") != "accepted":
    print(json.dumps({"harness_error": "submit_not_accepted", "result": submitted}))
    raise SystemExit(2)

lock = sqlite3.connect("/app/state/orders.db", timeout=5.0)
lock.execute("BEGIN IMMEDIATE")
worker = subprocess.Popen(
    ["python3", SERVICE, "worker"],
    text=True,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
)
interrupted = False
try:
    while True:
        line = worker.stdout.readline()
        if not line:
            worker.wait()
            print(json.dumps({
                "harness_error": "worker_exited_before_result",
                "code": worker.returncode,
                "stderr": worker.stderr.read(),
            }))
            raise SystemExit(2)
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            print(json.dumps({"harness_error": "invalid_worker_event", "line": line}))
            raise SystemExit(2)
        if event.get("order_id") == ORDER_ID and event.get("phase") == "result_ready":
            worker.terminate()
            worker.wait()
            interrupted = True
            break
finally:
    if worker.poll() is None:
        worker.kill()
        worker.wait()
    lock.rollback()
    lock.close()

if not interrupted:
    print(json.dumps({"harness_error": "interruption_not_reached"}))
    raise SystemExit(2)

retry = run(["worker"])
if retry.returncode != 0:
    print(json.dumps({
        "harness_error": "retry_failed",
        "code": retry.returncode,
        "stderr": retry.stderr.strip(),
    }))
    raise SystemExit(2)

state = one_json(["report", ORDER_ID])
packages = state["packages"]
order = state["order"]
violations = []
if len(packages) != 1:
    violations.append({
        "invariant": "each order reserves at most one package",
        "observed": len(packages),
    })
if state["queue_waiting"] != 0:
    violations.append({
        "invariant": "work with a persisted result is no longer waiting",
        "observed": state["queue_waiting"],
    })
if order["status"] != "fulfilled":
    violations.append({
        "invariant": "an order with no queued work is fulfilled",
        "observed": order["status"],
    })
if order["result"] is None:
    violations.append({
        "invariant": "a fulfilled order records its reservation result",
        "observed": None,
    })
elif packages and order["result"]["package_id"] != packages[0]["package_id"]:
    violations.append({
        "invariant": "the order result identifies its reservation",
        "observed": order["result"]["package_id"],
    })
elif state["cache"] is not None and state["cache"]["result"] != order["result"]:
    violations.append({
        "invariant": "cached and stored results agree",
        "observed": state["cache"]["result"],
    })

print(json.dumps({
    "order_id": ORDER_ID,
    "order_status": order["status"],
    "package_count": len(packages),
    "queue_waiting": state["queue_waiting"],
    "violations": violations,
}, sort_keys=True))
raise SystemExit(1 if violations else 0)
PY
