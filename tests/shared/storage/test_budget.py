"""The spend cap (shared/storage/budget.py): SDE_BUDGET_USD and a batch's budget_usd stop
paid claims; unpaid jobs keep moving; a raised cap resumes the held-back jobs."""

import logging

import pytest
from tests.shared.conftest import add_task

from shared.storage import budget
from shared.storage.sql import insert_job, utcnow
from shared.contracts.models import JobType

ROLLOUT_EST = budget.ESTIMATE_USD[JobType.ROLLOUT]


def spent(backend, task_id: str, usd: float, n: int = 1) -> None:
    """`n` settled jobs that together cost `usd`."""
    with backend.store.tx() as c:
        for i in range(n):
            insert_job(c, id=f"{task_id}-paid{i}", task_id=task_id, version=1, type=JobType.ROLLOUT, slot=90 + i)
            c.execute("UPDATE jobs SET status = 'DONE', cost_usd = ? WHERE id = ?", (usd / n, f"{task_id}-paid{i}"))


def ready(backend, task_id: str, job_type: JobType, name: str, **kw) -> str:
    jid = f"{task_id}-{name}"
    with backend.store.tx() as c:
        insert_job(c, id=jid, task_id=task_id, version=1, type=job_type, **kw)
    backend.queue.publish([{"id": jid, "type": job_type, "due_at": 0}])
    return jid


def claim(jq, job_type):
    return jq.claim([job_type], "w")


@pytest.fixture
def cap(monkeypatch):
    def set_cap(usd):
        monkeypatch.setenv("SDE_BUDGET_USD", str(usd))
        monkeypatch.setattr(budget, "_last_warn", float("-inf"))
    return set_cap


@pytest.mark.parametrize("value", ["none", "Off", "unlimited", " "])
def test_none_means_no_cap(cap, value):
    cap(value)  # SSM can't store an empty string, so the deploy passes "none"
    assert budget.cap_usd() is None


def test_no_cap_by_default(backend, jq, monkeypatch):
    monkeypatch.delenv("SDE_BUDGET_USD", raising=False)
    t = add_task(backend)
    spent(backend, t, 1_000.0)
    ready(backend, t, JobType.ROLLOUT, "r1", slot=1)
    assert claim(jq, JobType.ROLLOUT) is not None
    assert budget.status(backend.store.conn)["budget_exhausted"] is False


def test_paid_claims_stop_at_the_cap(backend, jq, cap, caplog):
    cap(1.0)
    t = add_task(backend)
    spent(backend, t, 0.99)  # $0.01 left: less than any paid job's estimate
    ready(backend, t, JobType.ROLLOUT, "r1", slot=1)
    with caplog.at_level(logging.ERROR, logger="budget"):
        assert claim(jq, JobType.ROLLOUT) is None
    assert "BUDGET EXHAUSTED" in caplog.text
    s = budget.status(backend.store.conn)
    assert s["cap_usd"] == 1.0 and s["budget_exhausted"] is True
    assert backend.store.job(f"{t}-r1")["status"] == "READY"  # held back, not dropped

    cap(2.0)  # raising the cap resumes it
    assert claim(jq, JobType.ROLLOUT)["id"] == f"{t}-r1"


def test_leased_jobs_count_by_their_estimate(backend, jq, cap):
    cap(3 * ROLLOUT_EST + 0.001)  # room for three rollouts
    t = add_task(backend)
    for s in range(1, 5):
        ready(backend, t, JobType.ROLLOUT, f"r{s}", slot=s)
    got = [claim(jq, JobType.ROLLOUT) for _ in range(4)]
    assert sum(j is not None for j in got) == 3  # the 4th would pass the cap while three are leased
    assert budget.status(backend.store.conn)["leased_estimate_usd"] == pytest.approx(3 * ROLLOUT_EST)


def test_concurrent_claimers_never_pass_the_cap(backend, cap):
    """16 threads, each with its own connection, race for 40 rollouts with room for 5."""
    import threading

    from shared.jobs.jobqueue import JobQueue

    cap(5 * ROLLOUT_EST + 0.001)
    t = add_task(backend)
    for s in range(1, 41):
        ready(backend, t, JobType.ROLLOUT, f"r{s}", slot=s)
    got, start = [], threading.Barrier(16)

    def worker(i):
        jq = JobQueue(backend)
        start.wait()
        for _ in range(5):
            if (job := jq.claim([JobType.ROLLOUT], f"w{i}")) is not None:
                got.append(job["id"])

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(16)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert len(got) == 5 == len(set(got))


def test_unpaid_jobs_continue_past_the_cap(backend, jq, cap):
    cap(0)
    t = add_task(backend)
    spent(backend, t, 5.0)
    ready(backend, t, JobType.WORLD_CHECK, "wc", input={"world_sha256": "w1"})
    assert JobType.WORLD_CHECK not in budget.PAID_JOB_TYPES
    assert claim(jq, JobType.WORLD_CHECK) is not None
    if JobType.VALIDATE not in budget.PAID_JOB_TYPES:  # agent mode: the leak review was paid at create
        ready(backend, t, JobType.VALIDATE, "v", input={"bundle_sha256": "b1"})
        assert claim(jq, JobType.VALIDATE) is not None


def test_a_mixed_claim_skips_only_the_paid_types(backend, jq, cap):
    cap(0.001)
    t = add_task(backend)
    ready(backend, t, JobType.ROLLOUT, "r1", slot=1, priority=100)
    ready(backend, t, JobType.WORLD_CHECK, "wc")
    job = jq.claim([JobType.ROLLOUT, JobType.WORLD_CHECK], "w")
    assert job["id"] == f"{t}-wc"


# --- the batch cap ----------------------------------------------------------------------


def batch(backend, batch_id: str, budget_usd: float | None, tasks: int = 2) -> list[str]:
    now = utcnow()
    with backend.store.tx() as c:
        c.execute("INSERT INTO batches (id, budget_usd, created_at) VALUES (?, ?, ?)", (batch_id, budget_usd, now))
        for i in range(1, tasks + 1):
            c.execute("INSERT INTO tasks (id, status, stage, batch_id, created_at, updated_at)"
                      " VALUES (?, 'CALIBRATING', 'QUEUED', ?, ?, ?)", (f"{batch_id}-t{i:02d}", batch_id, now, now))
    return [f"{batch_id}-t{i:02d}" for i in range(1, tasks + 1)]


def test_a_batch_cap_holds_back_only_that_batch(backend, jq, monkeypatch):
    monkeypatch.delenv("SDE_BUDGET_USD", raising=False)
    capped = batch(backend, "b-small", 0.5)
    free = batch(backend, "b-free", None)
    spent(backend, capped[0], 0.5)  # spent on one task counts for its sibling too
    ready(backend, capped[1], JobType.ROLLOUT, "r1", slot=1, priority=100)
    ready(backend, free[0], JobType.ROLLOUT, "r1", slot=1)
    assert claim(jq, JobType.ROLLOUT)["id"] == f"{free[0]}-r1"
    assert claim(jq, JobType.ROLLOUT) is None
    s = budget.status(backend.store.conn, "b-small")
    assert s == {"cap_usd": 0.5, "spent_usd": 0.5, "leased_estimate_usd": 0.0, "remaining_usd": 0.0,
                 "budget_exhausted": True}
    assert budget.status(backend.store.conn, "b-free")["budget_exhausted"] is False


def test_the_tighter_of_the_two_caps_wins(backend, jq, cap):
    cap(100.0)
    [t] = batch(backend, "b", 0.01, tasks=1)
    ready(backend, t, JobType.ROLLOUT, "r1", slot=1)
    assert claim(jq, JobType.ROLLOUT) is None  # the batch's $0.01 < one rollout's estimate
    cap(0.01)
    with backend.store.tx() as c:
        c.execute("UPDATE batches SET budget_usd = 100 WHERE id = 'b'")
    assert claim(jq, JobType.ROLLOUT) is None  # now the pipeline's
    cap(100.0)
    assert claim(jq, JobType.ROLLOUT) is not None


def test_a_bad_cap_is_refused(monkeypatch):
    monkeypatch.setenv("SDE_BUDGET_USD", "-1")
    with pytest.raises(ValueError):
        budget.cap_usd()
