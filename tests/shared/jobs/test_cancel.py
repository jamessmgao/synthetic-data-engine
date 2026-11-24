"""A user's cancel (shared/jobs/tasks.cancel): at any stage, nothing more is claimed or
handed off, what was in flight still records its cost, and the task stays CANCELLED."""

import importlib.util
from pathlib import Path

import pytest
from tests.shared.conftest import add_task

from shared.jobs import handoffs, tasks
from shared.storage import budget
from shared.storage.sql import insert_job
from shared.contracts.models import JobType, RejectReason, Stage

ROOT = Path(__file__).resolve().parents[3]

# (stage the task is in, the job running in it, its OK settle, the hand-off after it)
STAGES = {
    "world": (Stage.WORLD_BUILDING, JobType.WORLD, {"result": {"world_sha256": "w1"}}, handoffs.after_world),
    "world_check": (Stage.WORLD_CHECKING, JobType.WORLD_CHECK, {"result": {}}, handoffs.after_world_check),
    "create": (Stage.GENERATING, JobType.CREATE, {"bundle_sha256": "b1", "result": {}}, handoffs.after_generation),
    "validate": (Stage.VALIDATING, JobType.VALIDATE, {"result": {"gates": {}}}, handoffs.after_validate),
    "rollout": (Stage.EVALUATING, JobType.ROLLOUT, {"reward": 1.0}, None),
    "analyze": (Stage.EVALUATING, JobType.ANALYZE, {"result": {"action": "accept"}}, None),
}
INPUTS = {JobType.WORLD_CHECK: {"world_sha256": "w1", "spec": {}, "round": 0},
          JobType.VALIDATE: {"bundle_sha256": "b1", "round": 0}}


def job_name(job_type):
    return {JobType.VALIDATE: "validate-r0", JobType.WORLD_CHECK: "world-check-r0"}.get(job_type, str(job_type))


@pytest.fixture(autouse=True)
def no_cap(monkeypatch):
    monkeypatch.delenv("SDE_BUDGET_USD", raising=False)


def jobs(backend, t):
    return {j["id"]: j for j in backend.store.task_jobs(t)}


@pytest.mark.parametrize("where", list(STAGES))
def test_cancel_during_each_stage_stops_it_there(backend, jq, where):
    stage, job_type, ok, handoff = STAGES[where]
    t = add_task(backend)
    running, waiting = f"{t}-v1-{job_name(job_type)}", f"{t}-v1-waiting"
    with backend.store.tx() as c:
        c.execute("UPDATE tasks SET status = 'CALIBRATING', stage = ? WHERE id = ?", (str(stage), t))
        insert_job(c, id=running, task_id=t, version=1, type=job_type, slot=1, input=INPUTS.get(job_type))
        insert_job(c, id=waiting, task_id=t, version=1, type=job_type, slot=2, input=INPUTS.get(job_type))
    jq.publish([running, waiting])  # Redis only sees published jobs
    job = jq.claim([job_type], "w")
    assert job["id"] == running

    with backend.store.tx() as c:
        out = tasks.cancel(c, t, "user", "stop the batch")
    assert out == {"task_id": t, "cancelled": True, "status": "CANCELLED", "closed_jobs": 2}
    assert jq.claim([job_type], "w2") is None  # the waiting job is never claimed
    assert not jq.heartbeat(running, job["lease_token"])  # the running worker learns it lost the job

    # The running job finishes anyway: its settle is fenced, nothing is handed off or
    # enqueued, but what it spent is recorded.
    before = set(jobs(backend, t))
    fields = {"outcome": "OK", "cost_usd": 1.5, **ok}
    assert not jq.settle(running, job["lease_token"], fields,
                         followups=({"id": f"{t}-v1-next", "type": JobType.ANALYZE, "task_id": t, "version": 1},),
                         then=handoff(backend) if handoff else None)
    after = jobs(backend, t)
    assert set(after) == before
    assert after[running]["status"] == "DEAD" and after[running]["error"].startswith("cancelled: task CANCELLED")
    assert after[running]["cost_usd"] == 1.5 and after[running]["outcome"] is None
    assert budget.status(backend.store.conn)["spent_usd"] == 1.5

    task = backend.store.task(t)
    assert (task["status"], task["stage"], task["reason"]) == ("CANCELLED", "CANCELLED", "cancelled_by_user")
    assert task["cancelled_by"] == "user" and task["cancel_note"] == "stop the batch" and task["cancelled_at"]


def test_a_worker_whose_job_is_cancelled_mid_run_records_the_cost_and_hands_off_nothing(backend, jq):
    """The user cancels while the job function runs; the heartbeat is refused (lost) before
    it returns. The worker still offers the fenced settle, so the spend is kept."""
    import time

    from shared.jobs import worker

    t = add_task(backend)
    with backend.store.tx() as c:
        c.execute("UPDATE tasks SET status = 'CALIBRATING', stage = 'GENERATING' WHERE id = ?", (t,))
        insert_job(c, id=f"{t}-v1-create", task_id=t, version=1, type=JobType.CREATE)
    jq.publish([f"{t}-v1-create"])  # Redis only sees published jobs
    job = jq.claim([JobType.CREATE], "w")

    def authoring(j):
        with backend.store.tx() as c:
            tasks.cancel(c, t, "user")
        time.sleep(0.3)  # several heartbeats: the worker sees it lost the job
        return {"outcome": "OK", "cost_usd": 0.8, "bundle_sha256": "b1", "result": {}}

    assert not worker.run_one(jq, job, authoring, handoffs.after_generation(backend), heartbeat_sec=0.05)
    rows = jobs(backend, t)
    assert list(rows) == [f"{t}-v1-create"]  # no validate job
    assert rows[f"{t}-v1-create"]["cost_usd"] == 0.8


def test_a_cancel_stops_the_running_authoring_trial(backend, jq, monkeypatch):
    """When the heartbeat is refused, on_lost stops the trial: the job function returns at
    once instead of running to its time box (an authoring trial runs up to 15 min)."""
    import threading
    import time

    from shared.jobs import worker

    t = add_task(backend)
    with backend.store.tx() as c:
        c.execute("UPDATE tasks SET status = 'CALIBRATING', stage = 'WORLD_BUILDING' WHERE id = ?", (t,))
        insert_job(c, id=f"{t}-v1-world", task_id=t, version=1, type=JobType.WORLD)
    jq.publish([f"{t}-v1-world"])
    job = jq.claim([JobType.WORLD], "w")
    stopped = threading.Event()

    def trial(j):  # stands in for run_world_job: runs until its trial is stopped
        with backend.store.tx() as c:
            tasks.cancel(c, t, "user")
        assert stopped.wait(10), "the trial was never stopped"
        return {"outcome": "FAILED", "error": "the authoring trial was stopped", "cost_usd": 0.2,
                "result": {"failure_kind": "infra"}}

    started = time.monotonic()
    assert not worker.run_one(jq, job, trial, handoffs.after_world(backend), heartbeat_sec=0.05,
                              on_lost=lambda j: stopped.set())
    assert time.monotonic() - started < 5
    assert jobs(backend, t)[f"{t}-v1-world"]["cost_usd"] == 0.2  # what it spent is kept


def test_the_generation_service_stops_lost_authoring_trials(monkeypatch):
    """serve_generation wires on_lost to the harness's stop_trial in agent mode."""
    from shared.contracts import models
    from shared.jobs import worker

    seen = {}
    monkeypatch.setattr(models, "AUTHOR_MODE", "agent")
    monkeypatch.setattr(worker, "AUTHOR_MODE", "agent")
    monkeypatch.setattr(worker, "serve", lambda *a, **kw: seen.update(kw))
    worker.serve_generation(type("B", (), {"store": None, "queue": None, "artifacts": None})(), lambda j: {})
    assert seen["on_lost"] is worker.stop_authoring_trial
    from generator import authoring_harness

    called = []
    monkeypatch.setattr(authoring_harness, "stop_trial", lambda jid: called.append(jid) or True)
    worker.stop_authoring_trial({"id": "t-v1-world"})
    assert called == ["t-v1-world"]


def test_a_reject_or_accept_after_a_cancel_is_a_no_op(backend):
    t = add_task(backend)
    with backend.store.tx() as c:
        c.execute("UPDATE tasks SET stage = 'EVALUATING' WHERE id = ?", (t,))
        tasks.cancel(c, t, "user")
        assert not tasks.reject(c, t, RejectReason.INFRA_UNRESOLVED, Stage.UNRESOLVED)
        assert not tasks.accept(c, t, 1)
        assert not tasks.cas(c, t, version=1, stage=Stage.QUEUED)
    assert backend.store.task(t)["status"] == "CANCELLED"


def test_cancel_is_idempotent_and_leaves_decided_tasks_alone(backend):
    t, done = add_task(backend), add_task(backend, status="ACCEPTED")
    with backend.store.tx() as c:
        first = tasks.cancel(c, t, "user")
        again = tasks.cancel(c, t, "someone else")
        decided = tasks.cancel(c, done, "user")
    assert first["cancelled"] and not again["cancelled"] and again["status"] == "CANCELLED"
    assert backend.store.task(t)["cancelled_by"] == "user"  # the first cancel is the record
    assert not decided["cancelled"] and decided["status"] == "ACCEPTED"


def test_the_queue_drops_a_cancelled_tasks_jobs(backend, jq):
    t = add_task(backend)
    with backend.store.tx() as c:
        insert_job(c, id=f"{t}-v1-create", task_id=t, version=1, type=JobType.CREATE)
        tasks.cancel(c, t, "user")
    assert backend.store.claimable_ids() == []


# --- scripts/relabel_cancelled.py ----------------------------------------------------------


def relabel_script():
    spec = importlib.util.spec_from_file_location("relabel_cancelled", ROOT / "scripts" / "relabel_cancelled.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_relabel_turns_infra_unresolved_rejects_into_cancels(backend, capsys):
    stopped, failed, open_ = add_task(backend), add_task(backend), add_task(backend)
    with backend.store.tx() as c:
        tasks.reject(c, stopped, RejectReason.INFRA_UNRESOLVED, Stage.UNRESOLVED)
        tasks.reject(c, failed, RejectReason.INVALID, Stage.INVALID)
    rejected_at = backend.store.task(stopped)["updated_at"]
    script = relabel_script()

    script.main(["--task-ids", stopped, failed, open_, "missing"], backend=backend)  # a dry run
    out = capsys.readouterr().out
    assert "would become CANCELLED" in out and "nothing changed" in out
    assert backend.store.task(stopped)["status"] == "REJECTED"

    script.main(["--task-ids", stopped, failed, open_, "--execute", "--note", "stopped aws-b1"], backend=backend)
    out = capsys.readouterr().out
    assert "1 relabelled" in out
    row = backend.store.task(stopped)
    assert (row["status"], row["reason"], row["cancel_note"]) == ("CANCELLED", "cancelled_by_user", "stopped aws-b1")
    assert row["cancelled_at"] == rejected_at and row["cancelled_by"] == "operator"
    assert backend.store.task(failed)["status"] == "REJECTED"  # a real failure stays one
    assert backend.store.task(open_)["status"] == "CALIBRATING"
    script.main(["--task-ids", stopped, "--execute"], backend=backend)  # again: nothing to do
    assert "0 relabelled" in capsys.readouterr().out
