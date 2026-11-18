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


def test_poison_job_does_not_take_its_sibling_down(env, backend):
    """A job that kills its process every run is dead-lettered alone: the healthy
    job co-claimed by the same process is charged for at most one crash."""
    from shared.jobs.reconciler import reconcile

    poison, healthy = add_task(backend, "b-x-poison"), add_task(backend, "b-x-healthy")
    jq = JobQueue(backend)
    with backend.store.tx() as c:
        for t in (poison, healthy):
            insert_job(c, id=f"{t}-v1-generate", task_id=t, version=1, type=JobType.GENERATE)
    jq.publish([f"{poison}-v1-generate", f"{healthy}-v1-generate"])

    worker = Path(__file__).with_name("poison_worker.py")
    full = {**os.environ, **env, "SDE_LEASE_TTL_S": "0.6", "SDE_HEARTBEAT_S": "0.1", "SDE_POLL_S": "0.05"}
    for _ in range(12):  # a supervisor restarting the crashed worker
        p = subprocess.run([sys.executable, str(worker)], env=full, timeout=60)
        if p.returncode == 0:
            break
        time.sleep(0.7)  # leases of the dead process expire
        reconcile(backend)  # dead-letters, and re-adds expired jobs to Redis
    else:
        raise AssertionError("worker never finished")

    h = backend.store.job(f"{healthy}-v1-generate")
    p_ = backend.store.job(f"{poison}-v1-generate")
    assert h["status"] == "DONE" and h["outcome"] == "OK"
    assert h["attempts"] <= 2
    assert p_["status"] == "DEAD" and p_["attempts"] == 3
