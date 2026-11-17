"""Two copies at once, and kill -9 mid-batch then restart (real processes)."""

import os
import signal
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

from tests.shared.conftest import add_task

from shared.storage.sql import insert_job, rollout_job_id
from shared.jobs.jobqueue import JobQueue
from shared.contracts.models import JobType

WORKER = Path(__file__).with_name("fake_worker.py")
N_JOBS = 24
RUN_S = 0.3


def spawn(env: dict[str, str], owner: str) -> subprocess.Popen:
    full = {**os.environ, **env, "SDE_LEASE_TTL_S": "1.5", "SDE_HEARTBEAT_S": "0.3"}
    return subprocess.Popen([sys.executable, str(WORKER), owner, str(RUN_S)], env=full)


def wait_all(procs: list[subprocess.Popen], timeout: float = 90) -> None:
    deadline = time.time() + timeout
    for p in procs:
        p.wait(timeout=max(1.0, deadline - time.time()))
        assert p.returncode == 0, f"worker exited {p.returncode}"


def test_kill_and_restart_with_two_copies(env, backend):
    t = add_task(backend)
    jq = JobQueue(backend)
    ids = [rollout_job_id(t, 1, s, 0) for s in range(1, N_JOBS + 1)]
    with backend.store.tx() as c:
        for s, jid in enumerate(ids, 1):
            insert_job(c, id=jid, task_id=t, version=1, type=JobType.ROLLOUT, slot=s)
    jq.publish(ids)

    a, b = spawn(env, "a"), spawn(env, "b")  # two copies from the start
    time.sleep(1.5)
    a.send_signal(signal.SIGKILL)  # mid-run: it holds a lease and maybe a half-done run
    a.wait()
    a2 = spawn(env, "a2")  # restart
    wait_all([b, a2])

    jobs = {j["id"]: j for j in backend.store.task_jobs(t)}
    assert set(jobs) == set(ids)
    assert all(j["status"] == "DONE" and j["reward"] == 1.0 for j in jobs.values())

    runs = backend.work_dir / "runs"
    finished = Counter()
    started = Counter()
    for jid in ids:
        events = [line.split()[0] for line in (runs / f"{jid}.log").read_text().splitlines()]
        finished[jid] = events.count("finish")
        started[jid] = events.count("start")
    # No run that finished was ever run again: a reclaimed job adopts the result.
    assert max(finished.values()) == 1
    # Only the run in flight at the kill may have started twice.
    repeated = [jid for jid in ids if started[jid] > 1]
    assert len(repeated) <= 1, repeated
    for jid in repeated:
        assert jobs[jid]["attempts"] == 2
