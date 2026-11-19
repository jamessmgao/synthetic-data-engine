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
one_json(["submit", ORDER_ID, "widget", "3"])

lock = sqlite3.connect("/app/state/orders.db", timeout=5.0)
lock.execute("BEGIN IMMEDIATE")
worker = subprocess.Popen(
    ["python3", SERVICE, "worker"],
    text=True,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
)
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
            break
finally:
    if worker.poll() is None:
        worker.kill()
        worker.wait()
    lock.rollback()
    lock.close()

retry = run(["worker"])
if retry.returncode != 0:
    print(json.dumps({
        "harness_error": "retry_failed",
        "code": retry.returncode,
        "stderr": retry.stderr.strip(),
    }))
    raise SystemExit(2)

state = one_json(["report", ORDER_ID])
order = state["order"]
symptom = (
    order["status"] == "accepted"
    and state["package_count"] >= 1
    and state["queue_waiting"] == 0
)
print(json.dumps({
    "order_id": ORDER_ID,
    "order_status": order["status"],
    "package_count": state["package_count"],
    "queue_waiting": state["queue_waiting"],
    "symptom": symptom,
}, sort_keys=True))
raise SystemExit(1 if symptom else 0)
PY
