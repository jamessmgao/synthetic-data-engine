"""Claim, lease, fencing and settle semantics on every backend in the matrix."""

import threading
import time

from tests.shared.conftest import add_task

from shared.storage import from_env
from shared.storage.sql import insert_job, job_id, mark_handled, rollout_job_id
from shared.jobs.jobqueue import JobQueue
from shared.contracts.models import MAX_JOB_ATTEMPTS, JobType
from shared.jobs.reconciler import reconcile


def enqueue(backend, jq, task_id, type=JobType.GENERATE, n=1, **kw) -> list[str]:
    ids = []
    with backend.store.tx() as c:
        for i in range(n):
            jid = (rollout_job_id(task_id, 1, i + 1, 0) if type == JobType.ROLLOUT
                   else f"{task_id}-v1-{type}-{i}")
            insert_job(c, id=jid, task_id=task_id, version=1, type=type, slot=i + 1, **kw)
            ids.append(jid)
    jq.publish(ids)
    return ids


def test_insert_is_idempotent(backend, jq):
    t = add_task(backend)
    with backend.store.tx() as c:
        assert insert_job(c, id=job_id(t, 1, "generate"), task_id=t, version=1, type=JobType.GENERATE)
        assert not insert_job(c, id=job_id(t, 1, "generate"), task_id=t, version=1, type=JobType.GENERATE)
    assert len(backend.store.task_jobs(t)) == 1


def test_claim_leases_once_in_priority_order(backend, jq):
    t = add_task(backend)
    enqueue(backend, jq, t, JobType.GENERATE)
    enqueue(backend, jq, t, JobType.VALIDATE)
    enqueue(backend, jq, t, JobType.ROLLOUT)
    types = ["generate", "validate", "rollout"]
    got = [jq.claim(types, "w1")["type"] for _ in range(3)]
    assert got == ["rollout", "validate", "generate"]
    assert jq.claim(types, "w2") is None


def test_docker_slot_cap_spans_types_but_not_generate(backend, jq):
    t = add_task(backend)
    enqueue(backend, jq, t, JobType.ROLLOUT, n=3)
    enqueue(backend, jq, t, JobType.GENERATE)
    a = jq.claim("rollout", "w1", docker_slots=2)
    b = jq.claim("rollout", "w2", docker_slots=2)
    assert a and b
    assert jq.claim(["rollout", "validate"], "w3", docker_slots=2) is None
    assert jq.claim("generate", "w3", docker_slots=2)["type"] == "generate"
    assert jq.settle(a["id"], a["lease_token"], {"outcome": "VALID_PASS", "reward": 1.0})
    assert jq.claim("rollout", "w3", docker_slots=2) is not None


def test_expired_lease_is_taken_over_and_old_owner_is_fenced(backend, jq):
    t = add_task(backend)
    enqueue(backend, jq, t)
    old = jq.claim("generate", "w1", lease_sec=0.05)
    time.sleep(0.1)
    jq.queue.reconcile()  # Redis mode: the popped id comes back once its lease expires
    new = jq.claim("generate", "w2")
    assert new["id"] == old["id"] and new["lease_token"] == old["lease_token"] + 1
    assert new["attempts"] == 2
    assert not jq.heartbeat(old["id"], old["lease_token"])
    assert not jq.settle(old["id"], old["lease_token"], {"outcome": "OK"})
    assert not jq.release(old["id"], old["lease_token"])
    assert jq.heartbeat(new["id"], new["lease_token"])
    assert jq.settle(new["id"], new["lease_token"], {"outcome": "OK", "result": {"name": "x"}})
    job = backend.store.job(new["id"])
    assert job["status"] == "DONE" and job["lease_owner"] == "w2" and job["result"] == {"name": "x"}


def test_settle_inserts_followups_once_and_inherits_pinning(backend, jq):
    t = add_task(backend)
    [rid] = enqueue(backend, jq, t, JobType.ROLLOUT, bundle_sha256="sha-1", config_hash="cfg-1",
                    input={"task_dir": "/bundles/sha-1"})
    job = jq.claim("rollout", "w1")
    retry = {"id": rollout_job_id(t, 1, 1, 1), "type": "rollout", "task_id": t, "version": 1,
             "slot": 1, "retry": 1, "delay_sec": 0}
    assert jq.settle(job["id"], job["lease_token"], {"outcome": "INVALID", "error": "AgentTimeoutError"},
                     (retry,))
    assert not jq.settle(job["id"], job["lease_token"], {"outcome": "INVALID"}, (retry,))
    rows = {j["id"]: j for j in backend.store.task_jobs(t)}
    assert set(rows) == {rid, retry["id"]}
    r = rows[retry["id"]]
    assert (r["bundle_sha256"], r["config_hash"], r["input"]) == ("sha-1", "cfg-1", {"task_dir": "/bundles/sha-1"})
    assert jq.claim("rollout", "w2")["id"] == retry["id"]


def test_settled_jobs_wait_for_the_job_service(backend, jq):
    t = add_task(backend)
    enqueue(backend, jq, t, n=2)
    for _ in range(2):
        j = jq.claim("generate", "w1")
        jq.settle(j["id"], j["lease_token"], {"outcome": "OK"})
    pending = backend.store.unhandled()
    assert len(pending) == 2
    with backend.store.tx() as c:
        assert mark_handled(c, pending[0]["id"])
        assert not mark_handled(c, pending[0]["id"])
    assert [j["id"] for j in backend.store.unhandled()] == [pending[1]["id"]]


def test_settle_rejects_unknown_fields_and_can_dead_letter(backend, jq):
    t = add_task(backend)
    enqueue(backend, jq, t)
    j = jq.claim("generate", "w1")
    try:
        jq.settle(j["id"], j["lease_token"], {"outcome": "OK", "verdict": "nope"})
        raise AssertionError("expected ValueError")
    except ValueError:
        pass
    assert jq.settle(j["id"], j["lease_token"], {"status": "DEAD", "error": "TooManyClaims: 4"})
    assert backend.store.job(j["id"])["status"] == "DEAD"
    assert backend.store.unhandled()[0]["id"] == j["id"]


def test_release_returns_job_after_delay(backend, jq):
    t = add_task(backend)
    enqueue(backend, jq, t)
    j = jq.claim("generate", "w1")
    assert jq.release(j["id"], j["lease_token"], error="shutdown", delay_sec=0.2)
    assert jq.claim("generate", "w2") is None
    time.sleep(0.25)
    assert jq.claim("generate", "w2")["id"] == j["id"]


def test_jobs_of_decided_tasks_are_not_claimed(backend, jq):
    t = add_task(backend, status="REJECTED")
    enqueue(backend, jq, t)
    assert jq.claim("generate", "w1") is None


def test_batch_filter(backend, jq):
    a = add_task(backend, batch_id="b-a")
    b = add_task(backend, batch_id="b-b")
    enqueue(backend, jq, a)
    enqueue(backend, jq, b)
    assert jq.claim("generate", "w1", batch_id="b-b")["task_id"] == b


def test_reconciler_dead_letters_jobs_that_keep_killing_workers(backend, jq):
    t = add_task(backend)
    enqueue(backend, jq, t)
    for i in range(MAX_JOB_ATTEMPTS):
        jq.queue.reconcile()
        assert jq.claim("generate", f"w{i}", lease_sec=0.01) is not None
        time.sleep(0.02)
    assert jq.claim("generate", "w9") is None
    assert reconcile(backend)["dead_lettered"] == 1
    job = backend.store.unhandled()[0]
    assert job["status"] == "DEAD" and "dead-lettered" in job["error"]
    assert reconcile(backend)["dead_lettered"] == 0


def test_concurrent_claims_never_share_a_job(env, backend):
    t = add_task(backend)
    jq = JobQueue(backend)
    ids = enqueue(backend, jq, t, n=60)
    claimed: list[str] = []
    lock = threading.Lock()

    def worker(n: int) -> None:
        q = JobQueue(from_env(env))  # its own connections, like another process
        while (j := q.claim("generate", f"w{n}")) is not None:
            assert q.settle(j["id"], j["lease_token"], {"outcome": "OK"})
            with lock:
                claimed.append(j["id"])

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert sorted(claimed) == sorted(ids)
    assert all(j["attempts"] == 1 and j["status"] == "DONE" for j in backend.store.task_jobs(t))


def test_redis_queue_is_rebuilt_from_the_database(env, backend, jq):
    if "REDIS_URL" not in env:
        return
    t = add_task(backend)
    ids = enqueue(backend, jq, t, n=3)
    jq.queue.r.flushdb()  # Redis lost everything
    assert jq.claim("generate", "w1") is None
    assert jq.queue.reconcile() == 3
    got = {jq.claim("generate", "w1")["id"] for _ in range(3)}
    assert got == set(ids)
