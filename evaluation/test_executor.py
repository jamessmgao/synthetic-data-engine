"""Tests for the executor's job handling on the real store and queue (SQLite),
with Harbor replaced by a fake.

Run with: python -m pytest evaluation
"""

import os
import signal
import threading
import time
from pathlib import Path

import pytest

from evaluation import harbor, settings
from evaluation.classify import INVALID, VALID_FAIL, VALID_PASS
from evaluation.evaluate import summarize
from evaluation.executor import Executor, rollout_job
from shared.storage import from_env
from shared.storage.sql import insert_job, utcnow
from shared.jobs.jobqueue import JobQueue
from shared.contracts.models import JobType

AGENT = harbor.AgentConfig("terminus-2", "openrouter/z-ai/glm-5.3-flash", {"reasoning_effort": "high"})
SHA = "b" * 64


def trial_result(reward=None, exception=None) -> dict:
    return {
        "trial_name": "t__1", "task_checksum": "c0ffee",
        "started_at": "2026-11-17T22:00:00Z", "finished_at": "2026-11-17T22:05:00Z",
        "config": {"agent": {"name": AGENT.agent, "model_name": AGENT.model, "kwargs": AGENT.kwargs}},
        "agent_result": {"cost_usd": 0.01},
        "verifier_result": None if reward is None else {"rewards": {"reward": reward}},
        "exception_info": None if exception is None else {"exception_type": exception, "exception_message": ""},
    }


class FakeHarbor:
    """Returns scripted results per job id and records every call."""

    def __init__(self, script: dict[str, object] | None = None, default: dict | None = None, delay: float = 0.0):
        self.script = script or {}
        self.default = default or trial_result(1.0)
        self.delay = delay
        self.calls: list[str] = []
        self._lock = threading.Lock()

    def __call__(self, task_dir, job_id, agent, cancel, traces_dir=None):
        with self._lock:
            self.calls.append(job_id)
        if cancel.wait(self.delay):
            raise harbor.Cancelled()
        result = self.script.get(job_id, self.default)
        if isinstance(result, Exception):
            raise result
        return harbor.TrialRun(result=result, trial_dir=Path(traces_dir) / job_id / "t__1", adopted=False)


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(settings, "RETRY_BACKOFF_SEC", 0.0)


@pytest.fixture
def backend(tmp_path):
    return from_env({"SDE_WORK_DIR": str(tmp_path / "work")})


def seed(backend, runs=5, task_id="t1"):
    now = utcnow()
    with backend.store.tx() as c:
        c.execute("""INSERT INTO tasks (id, status, stage, created_at, updated_at)
                     VALUES (?, 'CALIBRATING', 'EVALUATING', ?, ?)""", (task_id, now, now))
        for slot in range(1, runs + 1):
            insert_job(c, id=rollout_job(task_id, 1, slot, SHA)["id"], task_id=task_id, version=1,
                       type=JobType.ROLLOUT, slot=slot, bundle_sha256=SHA)


def jobs(backend, task_id="t1") -> list[dict]:
    """The task's runs (not the analyze pings each settle adds for the Trace Parser)."""
    return sorted((j for j in backend.store.task_jobs(task_id) if j["type"] == "rollout"), key=lambda j: j["id"])


def expire_lease(backend, job_id):
    with backend.store.tx() as c:
        c.execute("UPDATE jobs SET lease_until = ? WHERE id = ?", (time.time() - 1, job_id))


def idle(backend):
    return lambda: all(j["status"] in ("DONE", "DEAD") for j in jobs(backend))


def make(backend, fake, **kw):
    kw = {"poll_sec": 0.01, "heartbeat_sec": 0.05, **kw}
    return Executor(backend, run_trial=fake, agent=AGENT, **kw)


def drain(ex):
    while ex.run_one("w"):
        pass


def test_settles_each_slot_with_outcome_reward_cost_and_trace(backend):
    seed(backend)
    fake = FakeHarbor({"t1-v1-rollout2-r0": trial_result(0.0)})
    drain(make(backend, fake))
    by_id = {j["id"]: j for j in jobs(backend)}
    assert by_id["t1-v1-rollout1-r0"]["outcome"] == VALID_PASS
    assert by_id["t1-v1-rollout2-r0"]["outcome"] == VALID_FAIL and by_id["t1-v1-rollout2-r0"]["reward"] == 0.0
    assert all(j["status"] == "DONE" and j["cost_usd"] == 0.01 for j in by_id.values())
    assert by_id["t1-v1-rollout1-r0"]["trace_uri"] == "traces/t1-v1-rollout1-r0/"
    # Explaining failures is the Analysis service's job, not the executor's.
    assert all(j["failure"] is None and j["trace_metrics"] is None for j in by_id.values())
    assert summarize(jobs(backend), 5)["verdict"] == "TOO_EASY"  # 4/5


def test_the_trial_runs_against_the_versions_bundle(backend):
    seed(backend, runs=1)
    seen = []

    def spy(task_dir, job_id, agent, cancel, traces_dir=None):
        seen.append((task_dir, traces_dir))
        return harbor.TrialRun(result=trial_result(1.0), trial_dir=Path(traces_dir) / job_id / "t", adopted=False)

    drain(make(backend, spy))
    assert seen == [(backend.artifacts.bundle_path(SHA), backend.artifacts.trace_dir("x").parent)]


def test_trace_is_fetched_before_running_and_uploaded_before_settling(backend, monkeypatch):
    """Cross-machine adoption: another machine's finished upload is fetched first; a
    finished trial is uploaded before the job settles, so a crash loses nothing."""
    seed(backend, runs=2)
    events = []
    art = backend.artifacts
    monkeypatch.setattr(art, "fetch_trace", lambda job_id: events.append(("fetch", job_id)) or job_id.endswith("1-r0"))
    monkeypatch.setattr(art, "abandon_trace", lambda job_id: events.append(("abandon", job_id)))
    monkeypatch.setattr(art, "save_trace", lambda job_id: events.append(("save", job_id)) or f"traces/{job_id}/")

    def fake(task_dir, job_id, agent, cancel, traces_dir=None):
        events.append(("run", job_id))
        return harbor.TrialRun(result=trial_result(0.0), trial_dir=Path(traces_dir) / job_id / "t", adopted=False)

    real_settle = JobQueue.settle

    def settle(self, job_id, *a, **kw):
        events.append(("settle", job_id))
        return real_settle(self, job_id, *a, **kw)

    monkeypatch.setattr(JobQueue, "settle", settle)
    drain(make(backend, fake))
    one, two = "t1-v1-rollout1-r0", "t1-v1-rollout2-r0"
    assert [e for e in events if e[1] == one] == [("fetch", one), ("run", one), ("save", one), ("settle", one)]
    # Nothing complete remotely for slot 2: half-uploaded files are set aside before running.
    assert [e for e in events if e[1] == two] == [("fetch", two), ("abandon", two), ("run", two),
                                                   ("save", two), ("settle", two)]


def test_invalid_rollout_is_retried_in_the_same_slot_never_scored(backend):
    seed(backend, runs=1)
    fake = FakeHarbor({"t1-v1-rollout1-r0": trial_result(0.0, exception="AgentTimeoutError")})
    drain(make(backend, fake))
    by_id = {j["id"]: j for j in jobs(backend)}
    assert by_id["t1-v1-rollout1-r0"]["outcome"] == INVALID and by_id["t1-v1-rollout1-r0"]["reward"] is None
    assert by_id["t1-v1-rollout1-r1"]["outcome"] == VALID_PASS
    assert by_id["t1-v1-rollout1-r1"]["bundle_sha256"] == SHA
    s = summarize(jobs(backend), 1)
    assert s["slots"][1]["outcome"] == VALID_PASS and s["invalid_attempts"] == 1


def test_retries_stop_after_the_limit_and_the_slot_stays_unresolved(backend, monkeypatch):
    monkeypatch.setattr("evaluation.executor.MAX_ROLLOUT_RETRIES", 2)
    monkeypatch.setattr(settings, "BREAKER_INVALID", 100)
    seed(backend, runs=1)
    drain(make(backend, FakeHarbor(default=trial_result(exception="RateLimitError"))))
    assert [j["id"] for j in jobs(backend)] == ["t1-v1-rollout1-r0", "t1-v1-rollout1-r1", "t1-v1-rollout1-r2"]
    assert summarize(jobs(backend), 1)["verdict"] == "UNRESOLVED"


def test_harbor_crash_is_invalid_and_retried(backend):
    seed(backend, runs=1)
    drain(make(backend, FakeHarbor({"t1-v1-rollout1-r0": harbor.HarborError("exit 1")})))
    by_id = {j["id"]: j for j in jobs(backend)}
    assert by_id["t1-v1-rollout1-r0"]["error"].startswith("HarborError")
    assert by_id["t1-v1-rollout1-r1"]["outcome"] == VALID_PASS


def test_a_heartbeat_that_raises_keeps_the_trial_running_and_settles(backend):
    """Seen on AWS: RDS out of connections made heartbeats raise, which killed the
    heartbeat thread; the lease lapsed under a running trial and it was run again."""
    seed(backend, runs=1)
    jq = JobQueue(backend)
    job = jq.claim(JobType.ROLLOUT, "w", 0.3)
    ex = make(backend, FakeHarbor(delay=0.8), lease_sec=0.3)
    real, calls = ex.queue.heartbeat, []

    def flaky(*a, **kw):
        calls.append(1)
        if len(calls) <= 2:
            raise ConnectionError("FATAL: remaining connection slots are reserved")
        return real(*a, **kw)

    ex.queue.heartbeat = flaky
    ex.process(job)
    assert len(calls) > 3
    row = jobs(backend)[0]
    assert row["status"] == "DONE" and row["outcome"] == VALID_PASS


def test_a_worker_that_lost_its_lease_records_nothing(backend):
    seed(backend, runs=1)
    jq = JobQueue(backend)
    job = jq.claim(JobType.ROLLOUT, "slow", 60)
    expire_lease(backend, job["id"])
    taker = jq.claim(JobType.ROLLOUT, "taker", 60)  # the job is taken over: new token
    assert taker["lease_token"] == job["lease_token"] + 1
    make(backend, FakeHarbor(delay=0.5), lease_sec=60).process(job)  # heartbeat fails, Harbor is cancelled
    row = jobs(backend)[0]
    assert row["status"] == "LEASED" and row["lease_owner"] == "taker" and row["outcome"] is None
    assert jq.settle(job["id"], job["lease_token"], {"outcome": VALID_FAIL}) is False  # fencing


def test_shutdown_releases_every_running_job_immediately(backend):
    seed(backend, runs=2)
    ex = make(backend, FakeHarbor(delay=30), heartbeat_sec=60)  # no heartbeat tick during the test
    server = threading.Thread(target=ex.serve, kwargs={"n_workers": 2})
    server.start()
    time.sleep(0.3)
    started = time.monotonic()
    ex.shutdown()
    server.join(timeout=5)
    assert not server.is_alive() and time.monotonic() - started < 2
    assert [(j["status"], j["outcome"]) for j in jobs(backend)] == [("READY", None), ("READY", None)]


def test_ctrl_c_waits_for_every_worker_to_release_its_job(backend):
    """Regression: KeyboardInterrupt inside Thread.join() let the process exit mid-cleanup."""
    seed(backend, runs=2)

    def slow_to_stop(task_dir, job_id, agent, cancel, traces_dir=None):
        cancel.wait()
        time.sleep(0.5)  # Harbor takes a moment to remove its containers
        raise harbor.Cancelled()

    ex = make(backend, slow_to_stop)
    threading.Timer(0.3, lambda: os.kill(os.getpid(), signal.SIGINT)).start()
    ex.serve(n_workers=2)  # on the main thread, like `evaluate`
    assert [j["status"] for j in jobs(backend)] == ["READY", "READY"]
    assert [t.name for t in threading.enumerate() if t.name.startswith("executor-")] == []


def test_two_executors_run_every_job_exactly_once(backend):
    seed(backend, runs=5)
    fake = FakeHarbor(delay=0.05)
    executors = [make(backend, fake) for _ in range(2)]
    threads = [threading.Thread(target=e.serve, kwargs={"n_workers": 3, "idle": idle(backend)}) for e in executors]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)
    assert sorted(fake.calls) == [f"t1-v1-rollout{s}-r0" for s in range(1, 6)]
    assert all(j["status"] == "DONE" for j in jobs(backend))


def test_docker_slot_cap_is_respected(backend):
    seed(backend, runs=5)
    running, peak = 0, 0
    lock = threading.Lock()

    def counting(task_dir, job_id, agent, cancel, traces_dir=None):
        nonlocal running, peak
        with lock:
            running += 1
            peak = max(peak, running)
        time.sleep(0.1)
        with lock:
            running -= 1
        return harbor.TrialRun(result=trial_result(1.0), trial_dir=Path(traces_dir) / job_id, adopted=False)

    make(backend, counting, docker_slots=2).serve(n_workers=5, idle=idle(backend))
    assert peak == 2


def test_breaker_pauses_claims_after_a_burst_of_invalid_rollouts(backend, monkeypatch):
    monkeypatch.setattr(settings, "BREAKER_INVALID", 2)
    seed(backend, runs=3)
    ex = make(backend, FakeHarbor(default=trial_result(exception="APIConnectionError")))
    ex.run_one("w")
    ex.run_one("w")
    assert ex.breaker.open_for() > 0
