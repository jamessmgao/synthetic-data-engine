"""Claim, lease, fencing and settle semantics on every backend in the matrix."""

import threading
import time

from tests.shared.conftest import add_task

from shared.storage import from_env
from shared.storage.sql import insert_job, job_id, mark_handled, rollout_job_id
from shared.jobs.jobqueue import JobQueue
from shared.contracts.models import MAX_JOB_ATTEMPTS, JobType
from shared.jobs.reconciler import reconcile


def enqueue(backend, jq, task_id, type=JobType.CREATE, n=1, **kw) -> list[str]:
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
        assert insert_job(c, id=job_id(t, 1, "create"), task_id=t, version=1, type=JobType.CREATE)
        assert not insert_job(c, id=job_id(t, 1, "create"), task_id=t, version=1, type=JobType.CREATE)
    assert len(backend.store.task_jobs(t)) == 1


def test_claim_leases_once_in_priority_order(backend, jq):
    t = add_task(backend)
    enqueue(backend, jq, t, JobType.CREATE)
    enqueue(backend, jq, t, JobType.VALIDATE)
    enqueue(backend, jq, t, JobType.ROLLOUT)
    types = ["create", "validate", "rollout"]
    got = [jq.claim(types, "w1")["type"] for _ in range(3)]
    assert got == ["rollout", "validate", "create"]
    assert jq.claim(types, "w2") is None


def test_docker_slot_cap_spans_types_but_not_create(backend, jq):
    t = add_task(backend)
    enqueue(backend, jq, t, JobType.ROLLOUT, n=3)
    enqueue(backend, jq, t, JobType.CREATE)
    a = jq.claim("rollout", "w1", docker_slots=2)
    b = jq.claim("rollout", "w2", docker_slots=2)
    assert a and b
    assert jq.claim(["rollout", "validate"], "w3", docker_slots=2) is None
    assert jq.claim("create", "w3", docker_slots=2)["type"] == "create"
    assert jq.settle(a["id"], a["lease_token"], {"outcome": "VALID_PASS", "reward": 1.0})
    assert jq.claim("rollout", "w3", docker_slots=2) is not None


def test_docker_slots_are_per_machine(backend, jq, monkeypatch):
    import shared.jobs.jobqueue as jqmod

    t = add_task(backend)
    enqueue(backend, jq, t, JobType.ROLLOUT, n=5)
    pid = jqmod.os.getpid()
    for host, expect in (("runner-a", 2), ("runner-b", 2)):
        monkeypatch.setattr(jqmod.socket, "gethostname", lambda h=host: h)
        got = [jq.claim("rollout", f"{host}:{pid}:{i}", docker_slots=2) for i in range(3)]
        assert sum(j is not None for j in got) == expect, host
    # The fifth job waits for a free slot on some machine.
    monkeypatch.setattr(jqmod.socket, "gethostname", lambda: "runner-c")
    assert jq.claim("rollout", f"runner-c:{pid}:0", docker_slots=2) is not None


def test_expired_lease_is_taken_over_and_old_owner_is_fenced(backend, jq):
    t = add_task(backend)
    enqueue(backend, jq, t)
    old = jq.claim("create", "w1", lease_sec=0.05)
    time.sleep(0.1)
    jq.queue.reconcile()  # Redis mode: the popped id comes back once its lease expires
    new = jq.claim("create", "w2")
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


def test_settled_jobs_can_be_marked_handled(backend, jq):
    t = add_task(backend)
    enqueue(backend, jq, t, n=2)
    for _ in range(2):
        j = jq.claim("create", "w1")
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
    j = jq.claim("create", "w1")
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
    j = jq.claim("create", "w1")
    assert jq.release(j["id"], j["lease_token"], error="shutdown", delay_sec=0.2)
    assert jq.claim("create", "w2") is None
    time.sleep(0.25)
    assert jq.claim("create", "w2")["id"] == j["id"]


def test_jobs_of_decided_tasks_are_not_claimed(backend, jq):
    t = add_task(backend, status="REJECTED")
    enqueue(backend, jq, t)
    assert jq.claim("create", "w1") is None


def test_reconciler_dead_letters_jobs_that_keep_killing_workers(backend, jq):
    t = add_task(backend)
    enqueue(backend, jq, t)
    for i in range(MAX_JOB_ATTEMPTS):
        jq.queue.reconcile()
        assert jq.claim("create", f"w{i}", lease_sec=0.01) is not None
        time.sleep(0.02)
    assert jq.claim("create", "w9") is None
    assert reconcile(backend)["dead_lettered"] == 1
    job = backend.store.unhandled()[0]
    assert job["status"] == "DEAD" and "dead-lettered" in job["error"]
    assert reconcile(backend)["dead_lettered"] == 0


def test_dead_lettered_rollout_triggers_one_analysis(backend, jq):
    t = add_task(backend)
    other = add_task(backend)
    [rid] = enqueue(backend, jq, t, JobType.ROLLOUT)
    [gid] = enqueue(backend, jq, other, JobType.CREATE)
    for i in range(MAX_JOB_ATTEMPTS):
        jq.queue.reconcile()
        for kind in ("rollout", "create"):
            assert jq.claim(kind, f"w{i}", lease_sec=0.01) is not None
        time.sleep(0.02)
    assert reconcile(backend)["dead_lettered"] == 2
    reconcile(backend)  # a second pass adds nothing
    jobs = {j["id"]: j for j in backend.store.task_jobs(t)}
    assert set(jobs) == {rid, f"{rid}-analyze"}  # rollouts only
    a = jobs[f"{rid}-analyze"]
    assert (a["type"], a["status"], a["version"], a["input"]) == ("analyze", "READY", 1, {"trigger": rid})
    assert jq.claim("analyze", "analyzer")["id"] == f"{rid}-analyze"
    # A dead create can't be handed on by anyone: its task is rejected instead.
    assert [j["id"] for j in backend.store.task_jobs(other)] == [gid]
    o = backend.store.task(other)
    assert (o["status"], o["reason"], o["stage"]) == ("REJECTED", "infra_unresolved", "UNRESOLVED")


def test_concurrent_claims_never_share_a_job(env, backend):
    t = add_task(backend)
    jq = JobQueue(backend)
    ids = enqueue(backend, jq, t, n=60)
    claimed: list[str] = []
    lock = threading.Lock()

    def worker(n: int) -> None:
        q = JobQueue(from_env(env))  # its own connections, like another process
        while (j := q.claim("create", f"w{n}")) is not None:
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


def test_a_column_added_by_another_process_does_not_break_old_connections(env, backend, jq):
    """Rolling deploy: an old process keeps querying while a new one migrates."""
    t = add_task(backend)
    enqueue(backend, jq, t)
    for _ in range(8):  # psycopg would auto-prepare after 5 runs
        assert backend.store.task_jobs(t)
    newer = from_env(env)  # another process, with a newer schema
    with newer.store.tx() as c:
        c.execute("ALTER TABLE jobs ADD COLUMN added_by_newer_release TEXT")
    rows = backend.store.task_jobs(t)  # the old connection still works
    assert "added_by_newer_release" in rows[0]


def test_redis_queue_is_rebuilt_from_the_database(env, backend, jq):
    if "REDIS_URL" not in env:
        return
    t = add_task(backend)
    ids = enqueue(backend, jq, t, n=3)
    for key in jq.queue.r.scan_iter(f"{jq.queue.prefix}*"):  # Redis lost everything
        jq.queue.r.delete(key)
    assert jq.claim("create", "w1") is None
    assert jq.queue.reconcile() == 3
    got = {jq.claim("create", "w1")["id"] for _ in range(3)}
    assert got == set(ids)


def test_schema_init_adds_a_nullable_batch_id(tmp_path):
    """Databases from when there were no batches gain tasks.batch_id (nullable, indexed);
    a second start changes nothing."""
    import sqlite3

    from shared.storage.sql import SqlStore

    db = tmp_path / "old.db"
    with sqlite3.connect(db) as c:
        c.execute("""CREATE TABLE tasks (id TEXT PRIMARY KEY, slot TEXT NOT NULL DEFAULT '{}',
                     status TEXT NOT NULL, reason TEXT, current_version INTEGER NOT NULL DEFAULT 1,
                     stage TEXT NOT NULL, bundle_sha256 TEXT, name TEXT, accepted_version INTEGER,
                     created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
        c.execute("INSERT INTO tasks (id, status, stage, created_at, updated_at)"
                  " VALUES ('t-old', 'CREATED', 'QUEUED', 'x', 'x')")
    for _ in range(2):
        store = SqlStore(f"sqlite:///{db}")
        assert store.task("t-old")["batch_id"] is None
    indexes = {r["name"] for r in store.conn.execute("PRAGMA index_list(tasks)").fetchall()}
    assert "tasks_batch" in indexes


def test_schema_init_relaxes_the_first_batch_releases_not_null(tmp_path):
    """The first batch release had batch_id NOT NULL; its rows keep their label and
    a single task (null batch) can be inserted afterwards."""
    import sqlite3

    from shared.storage.sql import SqlStore

    db = tmp_path / "old.db"
    with sqlite3.connect(db) as c:
        c.execute("""CREATE TABLE tasks (id TEXT PRIMARY KEY, batch_id TEXT NOT NULL, slot TEXT NOT NULL DEFAULT '{}',
                     status TEXT NOT NULL, reason TEXT, current_version INTEGER NOT NULL DEFAULT 1,
                     stage TEXT NOT NULL, bundle_sha256 TEXT, name TEXT, accepted_version INTEGER,
                     created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
        c.execute("INSERT INTO tasks (id, batch_id, status, stage, created_at, updated_at)"
                  " VALUES ('t-old', 'b-1', 'CREATED', 'QUEUED', 'x', 'x')")
    store = SqlStore(f"sqlite:///{db}")
    assert store.task("t-old")["batch_id"] == "b-1"
    with store.tx() as conn:
        conn.execute("INSERT INTO tasks (id, status, stage, created_at, updated_at)"
                     " VALUES ('t-new', 'CREATED', 'QUEUED', 'y', 'y')")
    assert [b["batch_id"] for b in store.batches()] == ["b-1"]


def test_schema_init_lifts_the_old_three_version_cap(tmp_path):
    """Databases from when tasks had at most 3 versions get the cap removed from the
    table (MAX_VERSIONS is enforced in code); rows and job references survive."""
    import sqlite3

    from shared.storage.sql import SqlStore

    db = tmp_path / "old.db"
    with sqlite3.connect(db) as c:
        c.execute("""CREATE TABLE tasks (id TEXT PRIMARY KEY, slot TEXT NOT NULL DEFAULT '{}',
                     status TEXT NOT NULL, reason TEXT,
                     current_version INTEGER NOT NULL DEFAULT 1 CHECK (current_version BETWEEN 1 AND 3),
                     stage TEXT NOT NULL, bundle_sha256 TEXT, name TEXT, accepted_version INTEGER,
                     created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
        c.execute("INSERT INTO tasks (id, status, stage, created_at, updated_at)"
                  " VALUES ('t-old', 'CALIBRATING', 'EVALUATING', 'x', 'x')")
    store = SqlStore(f"sqlite:///{db}")  # creates jobs (referencing tasks) and migrates
    with store.tx() as c:
        insert_job(c, id="t-old-v1-create", task_id="t-old", version=1, type=JobType.CREATE)
        c.execute("UPDATE tasks SET current_version = 7 WHERE id = 't-old'")
    assert store.task("t-old")["current_version"] == 7
    assert store.job("t-old-v1-create")["task_id"] == "t-old"
    SqlStore(f"sqlite:///{db}")  # running the migration again is a no-op


def test_postgres_lifts_the_old_three_version_cap_once(backend):
    """On Postgres the old check is replaced once; later starts leave it alone."""
    import pytest

    from shared.storage.sql import SqlStore

    url = backend.store.url
    if not url.startswith("postgresql"):
        pytest.skip("Postgres only")
    with backend.store.tx() as c:
        c.execute("ALTER TABLE tasks DROP CONSTRAINT tasks_current_version_check")
        c.execute("ALTER TABLE tasks ADD CONSTRAINT tasks_current_version_check"
                  " CHECK (current_version BETWEEN 1 AND 3)")

    def definition():
        return backend.store.conn.execute(
            "SELECT pg_get_constraintdef(oid) AS def FROM pg_constraint"
            " WHERE conname = 'tasks_current_version_check'").fetchone()["def"]

    SqlStore(url)
    assert definition() == "CHECK ((current_version >= 1))"
    SqlStore(url)  # a second runner booting: nothing to change
    assert definition() == "CHECK ((current_version >= 1))"
