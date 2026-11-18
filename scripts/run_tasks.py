"""
A batch run as a client: create a batch of N tasks through the API, wait for it,
report its yield.

One `POST /v1/batches` creates the tasks (`<batch>-tNN`, docs/PRD.md §4). The script
polls `GET /v1/batches/<batch>` until every task is ACCEPTED or REJECTED, then writes
`GET /v1/batches/<batch>/report` to output/reports/<batch>/summary.json and prints
its yield block.

    SDE_API_KEY=... python scripts/run_tasks.py --count 10 [--url http://localhost:8000]
                                               [--prompt "..."] [--batch run-1]
                                               [--area database_consistency [--area ...]]
                                               [--mechanism M] [--environment E] [--dial 1-3]

`--area` limits every task to one or more problem families (GET /v1/families lists
them); without it the whole taxonomy is sampled.

Rerunning the same command (same --batch and --count) after a crash or Ctrl-C
creates nothing new and picks up where the tasks are.
"""

import argparse
import json
import os
import sys
import time
import urllib.error
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
    parser.add_argument("--batch", default=f"run-{datetime.now(UTC):%Y%m%d-%H%M}", help="batch id")
    parser.add_argument("--area", action="append", help="problem family to sample within (repeatable)")
    parser.add_argument("--mechanism", help="only this mechanism")
    parser.add_argument("--environment", help="only this environment")
    parser.add_argument("--dial", type=int, choices=(1, 2, 3), help="only this difficulty dial")
    parser.add_argument("--poll", type=float, default=30.0, help="seconds between status checks")
    parser.add_argument("--timeout", type=float, default=6 * 3600)
    args = parser.parse_args()
    key = os.environ.get("SDE_API_KEY") or sys.exit("set SDE_API_KEY")

    body = {"count": args.count, "batch_id": args.batch}
    if args.prompt:
        body["prompt"] = args.prompt
    if args.area:
        body["area"] = args.area[0] if len(args.area) == 1 else args.area
    for key_ in ("mechanism", "environment", "dial"):
        if getattr(args, key_) is not None:
            body[key_] = getattr(args, key_)
    created = call(args.url, key, "POST", "/v1/batches", body)
    batch = created["batch_id"]
    print(f"{batch}: {len(created['task_ids'])} tasks {'created' if created['created'] else 'already exist'}",
          flush=True)

    deadline = time.time() + args.timeout
    while True:
        tasks = call(args.url, key, "GET", f"/v1/batches/{batch}")["tasks"]
        done = [t for t in tasks if t["status"] in FINAL]
        print(f"{datetime.now():%H:%M:%S}  {len(done)}/{len(tasks)} finished  "
              + "  ".join(f"{t['id'].rsplit('-', 1)[1]}:{t['status'][:3]}/{t['stage']}" for t in tasks),
              flush=True)
        if len(done) == len(tasks) or time.time() > deadline:
            break
        time.sleep(args.poll)

    report = call(args.url, key, "GET", f"/v1/batches/{batch}/report")
    out = Path(__file__).resolve().parent.parent / "output" / "reports" / batch
    out.mkdir(parents=True, exist_ok=True)
    (out / "summary.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    print(f"yield: {json.dumps(report['yield'])}")
    print(f"report: {out / 'summary.json'}")


if __name__ == "__main__":
    main()
