"""One step of the mock-LLM load test (docs/LOAD_TEST.md).

    python scripts/loadtest.py <step> <n_tasks> [--task-dir DIR] [--timeout S] [--no-inject] [--interval S]

Run it where the services' environment is (the API container on the control runner),
with the cluster deployed with SDE_MOCK_LLM=1. It injects n copies of the fixture task
(scripts/inject_task.py: validate, then 5 rollouts, then the Trace Parser, all real
code), samples the queue every 15 s, waits until every task is decided, and prints
JSON: wall time, throughput, and per job type the count, INVALID count, and median /
p95 queue wait and run time.
"""

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from shared.storage import from_env  # noqa: E402


def seconds(a: str | None, b: str | None) -> float | None:
    if not a or not b:
        return None
    return (datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds()


def pct(xs: list[float], q: float) -> float | None:
    xs = sorted(x for x in xs if x is not None)
    return round(xs[min(len(xs) - 1, int(q * len(xs)))], 1) if xs else None


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("step")
    p.add_argument("n", type=int)
    p.add_argument("--task-dir", type=Path, default=ROOT / "tests" / "fixtures" / "p1-local-1" / "task")
    p.add_argument("--timeout", type=float, default=3 * 3600)
    p.add_argument("--no-inject", action="store_true", help="report on a step's existing tasks")
    p.add_argument("--interval", type=float, default=0, help="seconds between injected tasks (0 = a burst)")
    args = p.parse_args()
    conn = from_env().store.conn
    prefix = f"lt-{args.step}-"

    t0 = time.time()
    for i in range(0 if args.no_inject else args.n):
        subprocess.run([sys.executable, str(ROOT / "scripts" / "inject_task.py"), str(args.task_dir),
                        f"{prefix}{i:03d}"], check=True, capture_output=True)
        if args.interval:
            time.sleep(args.interval)
    injected_s = time.time() - t0

    samples = []
    while time.time() - t0 < args.timeout:
        open_ = conn.execute("SELECT count(*) AS n FROM tasks WHERE id LIKE ? AND status NOT IN "
                             "('ACCEPTED', 'REJECTED', 'CANCELLED')", (prefix + "%",)).fetchone()["n"]
        depth = {r["type"]: r["n"] for r in conn.execute(
            "SELECT type, count(*) AS n FROM jobs WHERE task_id LIKE ? AND status = 'READY' GROUP BY type",
            (prefix + "%",)).fetchall()}
        leased = {r["type"]: r["n"] for r in conn.execute(
            "SELECT type, count(*) AS n FROM jobs WHERE status = 'LEASED' GROUP BY type").fetchall()}
        samples.append({"t": round(time.time() - t0), "open_tasks": open_, "ready": depth, "leased": leased})
        if open_ == 0:
            break
        time.sleep(15)
    wall = time.time() - t0

    jobs = conn.execute("SELECT type, outcome, error, created_at, started_at, finished_at FROM jobs "
                        "WHERE task_id LIKE ?", (prefix + "%",)).fetchall()
    by_type: dict[str, dict] = {}
    for j in jobs:
        d = by_type.setdefault(j["type"], {"n": 0, "invalid": 0, "wait": [], "run": []})
        d["n"] += 1
        d["invalid"] += j["outcome"] == "INVALID"
        d["wait"].append(seconds(j["created_at"], j["started_at"]))
        d["run"].append(seconds(j["started_at"], j["finished_at"]))
    tasks = {r["status"]: r["n"] for r in conn.execute(
        "SELECT status, count(*) AS n FROM tasks WHERE id LIKE ? GROUP BY status", (prefix + "%",)).fetchall()}
    print(json.dumps({
        "step": args.step, "tasks": args.n, "decided": tasks, "inject_s": round(injected_s),
        "wall_s": round(wall), "tasks_per_min": round(args.n / (wall / 60), 2),
        "peak_ready": {t: max(s["ready"].get(t, 0) for s in samples) for t in by_type},
        "peak_leased": {t: max(s["leased"].get(t, 0) for s in samples) for t in by_type},
        "jobs": {t: {"n": d["n"], "invalid": d["invalid"],
                     "wait_p50_s": pct(d["wait"], 0.5), "wait_p95_s": pct(d["wait"], 0.95),
                     "run_p50_s": pct(d["run"], 0.5), "run_p95_s": pct(d["run"], 0.95)}
                 for t, d in sorted(by_type.items())},
        "samples": samples,
    }, indent=1, default=str))


if __name__ == "__main__":
    main()
