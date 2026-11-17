"""A stand-in worker process for the kill/resume tests: claim → heartbeat → "run"
→ settle, like the executor, but the run is a sleep that writes a result file.

Like a real Harbor run, a finished result survives the worker: a worker that
reclaims the job adopts `results/<job_id>.json` instead of running again. Every
start and finish is appended to `runs/<job_id>.log` so the test can count them.

Usage: python fake_worker.py <owner> <run_seconds>   (backend from the env)
"""

import json
import os
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from shared.storage import from_env  # noqa: E402
from shared.jobs.jobqueue import JobQueue  # noqa: E402
from shared.contracts.models import HEARTBEAT_S, LEASE_TTL_S  # noqa: E402


def log(work: Path, job_id: str, event: str) -> None:
    with open(work / "runs" / f"{job_id}.log", "a") as f:
        f.write(f"{event} {os.getpid()}\n")


def main() -> None:
    owner, run_s = sys.argv[1], float(sys.argv[2])
    backend = from_env()
    jq = JobQueue(backend)
    work = backend.work_dir
    (work / "runs").mkdir(parents=True, exist_ok=True)
    (work / "results").mkdir(parents=True, exist_ok=True)
    while True:
        job = jq.claim("rollout", owner, lease_sec=LEASE_TTL_S, docker_slots=4)
        if job is None:
            counts = backend.store.open_job_counts()
            if not any(counts.values()):
                return
            backend.queue.reconcile()  # Redis mode: bring back jobs of dead workers
            time.sleep(0.1)
            continue
        stop = threading.Event()

        def beat() -> None:
            while not stop.wait(HEARTBEAT_S):
                jq.heartbeat(job["id"], job["lease_token"], LEASE_TTL_S)

        threading.Thread(target=beat, daemon=True).start()
        result_file = work / "results" / f"{job['id']}.json"
        if result_file.exists():
            log(work, job["id"], "adopt")
        else:
            log(work, job["id"], "start")
            time.sleep(run_s)
            tmp = result_file.with_suffix(f".tmp{os.getpid()}")
            tmp.write_text(json.dumps({"reward": 1.0}))
            os.replace(tmp, result_file)
            log(work, job["id"], "finish")
        stop.set()
        reward = json.loads(result_file.read_text())["reward"]
        jq.settle(job["id"], job["lease_token"], {"outcome": "VALID_PASS", "reward": reward})


if __name__ == "__main__":
    main()
