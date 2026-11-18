"""The generic worker loop: settle on success, release on error, drop on lost lease."""

import threading
import time

from tests.shared.conftest import add_task

from shared.storage.sql import insert_job, job_id
from shared.contracts.models import JobType
from shared.jobs.worker import run_one, serve


def one_job(backend, jq) -> str:
    t = add_task(backend)
    jid = job_id(t, 1, "validate", 0)
    with backend.store.tx() as c:
        insert_job(c, id=jid, task_id=t, version=1, type=JobType.VALIDATE, bundle_sha256="sha")
    jq.publish([jid])
    return jid


def test_success_settles_with_the_functions_fields(backend, jq):
    jid = one_job(backend, jq)
    job = jq.claim("validate", "w1")
    assert run_one(jq, job, lambda j: {"outcome": "OK", "result": {"gates": {}}})
    row = backend.store.job(jid)
    assert row["status"] == "DONE" and row["outcome"] == "OK" and row["result"] == {"gates": {}}


def test_exception_releases_with_backoff(backend, jq):
    jid = one_job(backend, jq)
    job = jq.claim("validate", "w1")

    def boom(j):
        raise RuntimeError("docker daemon went away")

    assert not run_one(jq, job, boom)
    row = backend.store.job(jid)
    assert row["status"] == "READY" and "docker daemon" in row["error"]
    assert jq.claim("validate", "w2") is None  # backoff


def test_lost_lease_drops_the_result(backend, jq):
    jid = one_job(backend, jq)
    job = jq.claim("validate", "w1", lease_sec=0.05)

    def slow(j):
        time.sleep(0.2)
        jq.queue.reconcile()
        taken = jq.claim("validate", "w2")  # another worker takes the expired lease
        assert taken["id"] == jid
        return {"outcome": "OK"}

    assert not run_one(jq, job, slow, lease_sec=0.05, heartbeat_sec=10)
    row = backend.store.job(jid)
    assert row["status"] == "LEASED" and row["lease_owner"] == "w2"


def test_a_heartbeat_that_raises_is_retried_and_the_job_still_settles(backend, jq, monkeypatch):
    """A transient store error in a heartbeat must not kill the heartbeat thread: the lease
    would lapse under a running job and another worker would run it again."""
    jid = one_job(backend, jq)
    job = jq.claim("validate", "w1", lease_sec=0.3)
    real, calls = jq.heartbeat, []

    def flaky(*a, **kw):
        calls.append(time.monotonic())
        if len(calls) <= 2:
            raise ConnectionError("server closed the connection unexpectedly")
        return real(*a, **kw)

    monkeypatch.setattr(jq, "heartbeat", flaky)

    def outlives_its_lease(j):
        time.sleep(0.8)  # well past the 0.3s lease: only working heartbeats keep it
        jq.queue.reconcile()
        assert jq.claim("validate", "w2") is None  # nobody could take it
        return {"outcome": "OK"}

    assert run_one(jq, job, outlives_its_lease, lease_sec=0.3, heartbeat_sec=0.05)
    assert len(calls) > 3
    row = backend.store.job(jid)
    assert row["status"] == "DONE" and row["outcome"] == "OK"


def test_serve_respects_docker_slots_and_stops(backend, jq):
    t = add_task(backend)
    ids = [f"{t}-v1-validate-r{i}" for i in range(6)]
    with backend.store.tx() as c:
        for i in ids:
            insert_job(c, id=i, task_id=t, version=1, type=JobType.VALIDATE)
    jq.publish(ids)
    running, peak, lock = [0], [0], threading.Lock()

    def fn(j):
        with lock:
            running[0] += 1
            peak[0] = max(peak[0], running[0])
        time.sleep(0.1)
        with lock:
            running[0] -= 1
        return {"outcome": "OK"}

    stop = threading.Event()
    th = threading.Thread(target=serve, args=(jq, JobType.VALIDATE, fn),
                          kwargs={"concurrency": 4, "docker_slots": 2, "stop": stop})
    th.start()
    deadline = time.time() + 20
    while time.time() < deadline and backend.store.open_job_counts()["ready"] + backend.store.open_job_counts()["leased"]:
        time.sleep(0.05)
    stop.set()
    th.join()
    assert all(backend.store.job(i)["status"] == "DONE" for i in ids)
    assert peak[0] <= 2
