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


def test_free_memory_is_read_from_meminfo(tmp_path):
    from shared.jobs.jobqueue import free_memory_pct

    m = tmp_path / "meminfo"
    m.write_text("MemTotal:  8000000 kB\nMemFree:  100000 kB\nMemAvailable:  2000000 kB\n")
    assert free_memory_pct(str(m)) == 25.0
    assert free_memory_pct(str(tmp_path / "missing")) is None


def test_high_cpu_load_stops_new_docker_work(backend, jq, monkeypatch):
    import shared.jobs.jobqueue as jqmod

    t = add_task(backend)
    enqueue(backend, jq, t, JobType.ROLLOUT)
    monkeypatch.setattr(jqmod, "free_memory_pct", lambda: 60.0)
    monkeypatch.setattr(jqmod, "cpu_load_pct", lambda: 97.0)
    assert jq.claim("rollout", "w1", docker_slots=5) is None
    monkeypatch.setattr(jqmod, "cpu_load_pct", lambda: 40.0)
    assert jq.claim("rollout", "w1", docker_slots=5) is not None


def test_docker_claims_are_paced_per_process(backend, jq, monkeypatch):
    import shared.jobs.jobqueue as jqmod

    t = add_task(backend)
    enqueue(backend, jq, t, JobType.ROLLOUT, n=2)
    monkeypatch.setattr(jqmod, "free_memory_pct", lambda: 60.0)
    monkeypatch.setattr(jqmod, "cpu_load_pct", lambda: 10.0)
    monkeypatch.setattr(jqmod, "_last_docker_claim", [-1e9])
    monkeypatch.setattr(jqmod, "DOCKER_CLAIM_SPACING_S", 3600)
    assert jq.claim("rollout", "w1", docker_slots=5) is not None
    assert jq.claim("rollout", "w1", docker_slots=5) is None  # too soon after the last one
    monkeypatch.setattr(jqmod, "DOCKER_CLAIM_SPACING_S", 0)
    assert jq.claim("rollout", "w1", docker_slots=5) is not None


def test_cpu_load_reads_loadavg_and_is_none_off_linux(tmp_path, monkeypatch):
    import shared.jobs.jobqueue as jqmod

    monkeypatch.setattr(jqmod.os, "cpu_count", lambda: 2)
    f = tmp_path / "loadavg"
    f.write_text("1.80 1.20 0.90 3/400 12345\n")
    assert jqmod.cpu_load_pct(str(f)) == 90.0
    assert jqmod.cpu_load_pct(str(tmp_path / "missing")) is None


def test_a_suspect_is_not_starved_by_a_busy_process(backend, jq, monkeypatch):
    """An executor always holds a rollout, so crash isolation alone never let it take
    an abandoned one; after the wait cap it may (still isolated)."""
    import os
    import socket

    import shared.storage.sql as sql

    t = add_task(backend)
    enqueue(backend, jq, t, JobType.ROLLOUT, n=2)
    me = f"{socket.gethostname()}:{os.getpid()}:"
    dead = jq.claim("rollout", "dead-worker", lease_sec=0.01)  # its worker dies
    mine = jq.claim("rollout", me + "0")  # this process stays busy with the other one
    assert dead and mine
    time.sleep(0.05)
    jq.queue.reconcile()
    monkeypatch.setattr(sql, "SUSPECT_MAX_WAIT_S", 3600)
    assert jq.claim("rollout", me + "1") is None  # isolation: wait for an idle process
    monkeypatch.setattr(sql, "SUSPECT_MAX_WAIT_S", 0)
    got = jq.claim("rollout", me + "1")
    assert got["id"] == dead["id"] and got["isolated"] == 1


def test_low_memory_stops_new_docker_work_but_not_the_rest(backend, jq, monkeypatch):
    import shared.jobs.jobqueue as jqmod

    t = add_task(backend)
    enqueue(backend, jq, t, JobType.ROLLOUT)
    enqueue(backend, jq, t, JobType.CREATE, input={"origin": "initial", "world_sha256": "w"})
    enqueue(backend, jq, t, JobType.ANALYZE)
    monkeypatch.setattr(jqmod, "free_memory_pct", lambda: 12.0)
    monkeypatch.setattr(jqmod, "cpu_load_pct", lambda: 10.0)
    types = ["rollout", "create", "analyze"]
    got = jq.claim(types, "w1", docker_slots=5)
    assert got["type"] == "analyze"  # needs no Docker
    assert jq.claim(types, "w1", docker_slots=5) is None  # rollout and agent create wait for memory
    monkeypatch.setattr(jqmod, "free_memory_pct", lambda: 60.0)
    assert {jq.claim(types, "w1", docker_slots=5)["type"] for _ in range(2)} == {"rollout", "create"}


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


def test_dead_lettered_world_jobs_reject_their_tasks(backend, jq):
    """Agent mode: a world or world_check job nobody can finish must not leave its task in
    WORLD_BUILDING / WORLD_CHECKING forever (its batch would never complete)."""
    tasks_by_type = {}
    for kind in (JobType.WORLD, JobType.WORLD_CHECK):
        t = add_task(backend)
        enqueue(backend, jq, t, kind)
        tasks_by_type[kind] = t
    for i in range(MAX_JOB_ATTEMPTS):
        jq.queue.reconcile()
        for kind in tasks_by_type:
            assert jq.claim(str(kind), f"w{i}", lease_sec=0.01) is not None
        time.sleep(0.02)
    assert reconcile(backend)["dead_lettered"] == 2
    for t in tasks_by_type.values():
        task = backend.store.task(t)
        assert (task["status"], task["reason"]) == ("REJECTED", "infra_unresolved")


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


def test_literal_percent_in_sql_works_on_every_backend(backend, jq):
    t = add_task(backend)
    enqueue(backend, jq, t, n=2)
    rows = backend.store.conn.execute(
        "SELECT id FROM jobs WHERE id LIKE '%-0' AND task_id = ?", (t,)
    ).fetchall()
    assert [r["id"] for r in rows] == [f"{t}-v1-{JobType.CREATE}-0"]
    no_params = backend.store.conn.execute("SELECT COUNT(*) AS n FROM jobs WHERE id LIKE '%'").fetchone()
    assert no_params["n"] == 2


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


def test_many_threads_never_exceed_the_connection_pool(env, monkeypatch):
    """Worker loops, heartbeats and settles in one process share a bounded pool: 16
    threads working 64 jobs hold at most POOL_MAX connections (RDS ran out before)."""
    if "DATABASE_URL" not in env:
        return
    import psycopg

    import shared.storage.sql as sql

    monkeypatch.setattr(sql, "POOL_MAX", 3)  # a fresh pool: each test has its own database
    backend = from_env(env)
    t = add_task(backend)
    jq = JobQueue(backend)
    ids = enqueue(backend, jq, t, n=64)
    db = env["DATABASE_URL"].rsplit("/", 1)[1]
    peak, hb_peak, stop = [0], [0], threading.Event()

    def watch() -> None:
        q = "SELECT count(*) FROM pg_stat_activity WHERE datname = %s AND application_name = %s"
        with psycopg.connect(env["DATABASE_URL"], autocommit=True) as c:
            while not stop.wait(0.01):
                peak[0] = max(peak[0], c.execute(q, (db, "sde-pipeline")).fetchone()[0])
                hb_peak[0] = max(hb_peak[0], c.execute(q, (db, "sde-pipeline-heartbeat")).fetchone()[0])

    def work(n: int) -> None:
        while (j := jq.claim("create", f"w{n}")) is not None:
            assert jq.heartbeat(j["id"], j["lease_token"])
            assert jq.settle(j["id"], j["lease_token"], {"outcome": "OK"})

    watcher = threading.Thread(target=watch)
    watcher.start()
    threads = [threading.Thread(target=work, args=(n,)) for n in range(16)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    stop.set()
    watcher.join()
    assert all(backend.store.job(i)["status"] == "DONE" for i in ids)
    assert 1 <= peak[0] <= 3 and hb_peak[0] <= 1  # the main pool, plus one heartbeat connection


def test_heartbeats_go_through_while_the_main_pool_is_exhausted(env, monkeypatch):
    """Every main-pool connection is busy (long transactions): a heartbeat still lands."""
    if "DATABASE_URL" not in env:
        return
    import shared.storage.sql as sql

    monkeypatch.setattr(sql, "POOL_MAX", 1)
    monkeypatch.setattr(sql, "POOL_TIMEOUT_S", 0.5)
    backend = from_env(env)
    t = add_task(backend)
    jq = JobQueue(backend)
    enqueue(backend, jq, t)
    job = jq.claim("create", "w1")
    hold, held = threading.Event(), threading.Event()

    def busy() -> None:
        with backend.store.tx():
            held.set()
            hold.wait(5)

    th = threading.Thread(target=busy)
    th.start()
    held.wait(5)
    try:
        assert jq.heartbeat(job["id"], job["lease_token"])  # the heartbeat pool, not the busy one
    finally:
        hold.set()
        th.join()


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
