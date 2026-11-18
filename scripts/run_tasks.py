"""
A batch run as a client: create N tasks through the API, wait for them, report on them.

The system has no batches (docs/PRD.md §4): this script just sends N separate
`POST /v1/tasks` requests, remembers the ids, polls until every task is ACCEPTED
or REJECTED, then asks `GET /v1/report` for exactly those ids and writes it to
output/reports/<name>/summary.json.

    SDE_API_KEY=... python scripts/run_tasks.py --count 10 [--url http://localhost:8000]
                                               [--prompt "..."] [--name run-1]

Task ids are `<name>-tNN`, so rerunning the same command after a crash or Ctrl-C
creates nothing new and picks up where the tasks are.
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

FINAL = ("ACCEPTED", "REJECTED")


def call(url: str, key: str, method: str, path: str, body: dict | None = None):
    req = urllib.request.Request(
        url.rstrip("/") + path,
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={"X-Api-Key": key, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        sys.exit(f"{method} {path}: HTTP {e.code}: {e.read().decode(errors='replace')}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0].strip())
    parser.add_argument("--count", type=int, default=10)
    parser.add_argument("--url", default=os.environ.get("SDE_API_URL", "http://localhost:8000"))
    parser.add_argument("--prompt", default=None, help="seed every task with this idea (default: sampled specs)")
    parser.add_argument("--name", default=f"run-{datetime.now(UTC):%Y%m%d-%H%M}", help="prefix for task ids")
    parser.add_argument("--poll", type=float, default=30.0, help="seconds between status checks")
    parser.add_argument("--timeout", type=float, default=6 * 3600)
    args = parser.parse_args()
    key = os.environ.get("SDE_API_KEY") or sys.exit("set SDE_API_KEY")

    ids = []
    for i in range(1, args.count + 1):
        body = {"task_id": f"{args.name}-t{i:02d}"}
        if args.prompt:
            body["prompt"] = args.prompt
        created = call(args.url, key, "POST", "/v1/tasks", body)
        ids.append(created["task_id"])
        print(f"{created['task_id']}: {'created' if created['created'] else 'already exists'}", flush=True)

    deadline = time.time() + args.timeout
    while True:
        tasks = {t["id"]: call(args.url, key, "GET", f"/v1/tasks/{t}")["task"] for t in ids}
        done = [t for t in tasks.values() if t["status"] in FINAL]
        print(f"{datetime.now():%H:%M:%S}  {len(done)}/{len(ids)} finished  "
              + "  ".join(f"{t['id'].rsplit('-', 1)[1]}:{t['status'][:3]}/{t['stage']}" for t in tasks.values()),
              flush=True)
        if len(done) == len(ids) or time.time() > deadline:
            break
        time.sleep(args.poll)

    query = urllib.parse.urlencode([("task_id", t) for t in ids])
    report = call(args.url, key, "GET", f"/v1/report?{query}")
    out = Path(__file__).resolve().parent.parent / "output" / "reports" / args.name
    out.mkdir(parents=True, exist_ok=True)
    (out / "summary.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    print(f"report: {out / 'summary.json'}")


if __name__ == "__main__":
    main()
