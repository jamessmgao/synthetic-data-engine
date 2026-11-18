"""Workers ride out a database blip (shared/storage/dbretry.py): the queue calls are retried, the
claim loop never dies, and a job whose settle hits the blip still settles."""
import sqlite3
import threading

import pytest

from shared.jobs import worker
from shared.storage import dbretry


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(dbretry, "BACKOFF_START_S", 0.01)
    monkeypatch.setattr(dbretry, "BACKOFF_MAX_S", 0.02)
    monkeypatch.setattr(worker, "LOOP_ERROR_WAIT_S", 0.01)


def flaky(n, value="ok", exc=sqlite3.OperationalError("database is locked")):
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        if calls["n"] <= n:
            raise exc
        return value
    return fn, calls


def test_retry_rides_out_transient_errors():
    fn, calls = flaky(3)
    assert dbretry.retry(fn, "x") == "ok"
    assert calls["n"] == 4


def test_retry_gives_up_after_its_budget():
    fn, _ = flaky(10_000)
    with pytest.raises(sqlite3.OperationalError):
        dbretry.retry(fn, "x", budget_s=0.05)


def test_retry_stops_when_asked():
    stop = threading.Event()
    stop.set()
    fn, calls = flaky(10_000)
    with pytest.raises(sqlite3.OperationalError):
        dbretry.retry(fn, "x", stop=stop)
    assert calls["n"] == 1


def test_retry_raises_other_errors_at_once():
    fn, calls = flaky(1, exc=ValueError("a bug"))
    with pytest.raises(ValueError):
        dbretry.retry(fn, "x")
    assert calls["n"] == 1


def test_pool_timeout_is_transient():
    psycopg_pool = pytest.importorskip("psycopg_pool")
    assert issubclass(psycopg_pool.PoolTimeout, dbretry.TRANSIENT)


class FakeQueue:
    """claim / settle fail with a transient error the first times, then work."""

    def __init__(self, claim_failures=0, settle_failures=0):
        self.claim_failures, self.settle_failures = claim_failures, settle_failures
        self.jobs = [{"id": "j1", "lease_token": 1, "type": "t"}]
        self.settled, self.released = [], []
        self.stop = threading.Event()

    def claim(self, *a, **k):
        if self.claim_failures:
            self.claim_failures -= 1
            raise sqlite3.OperationalError("unable to open database file")
        if self.jobs:
            return self.jobs.pop()
        self.stop.set()
        return None

    def heartbeat(self, *a, **k):
        return True

    def settle(self, job_id, token, fields, then=None):
        if self.settle_failures:
            self.settle_failures -= 1
            raise sqlite3.OperationalError("database is locked")
        self.settled.append(job_id)
        return True

    def release(self, job_id, token, **k):
        self.released.append(job_id)
        return True


def test_claim_loop_survives_a_blip_and_the_job_settles():
    q = FakeQueue(claim_failures=3, settle_failures=2)
    worker.serve(q, "t", lambda job: {"outcome": "OK"}, stop=q.stop)
    assert q.settled == ["j1"] and q.released == []


def test_claim_loop_survives_an_unexpected_error():
    q = FakeQueue()
    real = q.claim
    calls = {"n": 0}

    def claim(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("not a database error")
        return real(*a, **k)
    q.claim = claim
    worker.serve(q, "t", lambda job: {"outcome": "OK"}, stop=q.stop)
    assert q.settled == ["j1"]
