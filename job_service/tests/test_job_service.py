"""The Job Service against a real SQLite store. Workers are simulated by settling
job rows directly, the way generate / validate / executor workers would."""

import json
import threading

import pytest

from job_service.service import BatchConflict, JobService
from job_service.taxonomy import UNIQUE_DIMENSIONS, sample_specs
from shared.storage import from_env
from shared.storage.sql import insert_job, job_id, rollout_job_id
from shared.contracts.models import JobType

T = "b-1-t01"


class RecordingQueue:
    def __init__(self):
        self.published: list[str] = []

    def publish(self, jobs):
        self.published += [j["id"] for j in jobs]


@pytest.fixture
def backend(tmp_path):
    b = from_env({"SDE_WORK_DIR": str(tmp_path / "work")})
    b.queue = RecordingQueue()
    return b


@pytest.fixture
def svc(backend, tmp_path):
    return JobService(backend, accepted_dir=tmp_path / "accepted")


def settle(svc, id, **fields):
    fields.setdefault("status", "DONE")
    fields = {k: json.dumps(v) if isinstance(v, (dict, list)) else v for k, v in fields.items()}
    cols = ", ".join(f"{k} = ?" for k in fields)
    with svc.store.tx() as conn:
        assert conn.execute(f"UPDATE jobs SET {cols} WHERE id = ?", (*fields.values(), id)).rowcount == 1


def task(svc, id=T):
    return svc.store.task(id)


def published(svc) -> list[str]:
    """Published task dirs (the artifact store stages copies under .staging/)."""
    return sorted(p.name for p in svc.accepted_dir.iterdir() if p.name != ".staging")


def job_ids(svc, id=T):
    return [j["id"] for j in svc.store.task_jobs(id)]


def build(svc, version, sha, name="queue-drain-bug", draft=None):
    """Generate settles OK with a bundle on disk; validate settles OK."""
    bundle = svc.artifacts.bundle_path(sha)
    bundle.mkdir(parents=True, exist_ok=True)
    (bundle / "instruction.md").write_text(f"fix it ({sha})\n")
    settle(svc, draft or job_id(T, version, "generate"), outcome="OK", bundle_sha256=sha,
           result={"name": name, "bundle_sha256": sha})
    svc.reconcile()
    settle(svc, job_id(T, version, "validate", 0), outcome="OK", result={"gates": {}})
    svc.reconcile()


def score(svc, version, pattern):
    for slot, mark in enumerate(pattern, start=1):
        settle(svc, rollout_job_id(T, version, slot, 0), reward=float(mark == "1"),
               outcome="VALID_PASS" if mark == "1" else "VALID_FAIL",
               failure=None if mark == "1" else {"explanation": f"run {slot} missed the retry path"})
    svc.reconcile()


# --- submission --------------------------------------------------------------


def test_submit_creates_tasks_and_generate_jobs(svc, backend):
    batch_id, ids, created = svc.submit_batch(3, "b-1")
    assert (batch_id, created) == ("b-1", True)
    assert ids == ["b-1-t01", "b-1-t02", "b-1-t03"]
    t = task(svc)
    assert (t["status"], t["stage"], t["current_version"]) == ("CREATED", "QUEUED", 1)
    gen = svc.store.job(job_id(T, 1, "generate"))
    assert gen["status"] == "READY" and gen["input"]["origin"] == "initial"
    assert gen["input"]["spec"] == t["slot"] and t["slot"]["taxonomy"]["domain"]
    assert backend.queue.published == [job_id(i, 1, "generate") for i in ids]


def test_submit_is_idempotent_and_conflicts_on_count(svc, backend):
    svc.submit_batch(2, "b-1")
    assert svc.submit_batch(2, "b-1")[2] is False
    assert backend.queue.published == [job_id("b-1-t01", 1, "generate"), job_id("b-1-t02", 1, "generate")]
    with pytest.raises(BatchConflict):
        svc.submit_batch(3, "b-1")
    assert len(svc.store.batch_tasks("b-1")) == 2


def test_specs_are_diverse_and_deterministic():
    specs = sample_specs("b-x", 12)
    combos = {tuple(s["taxonomy"][d] for d in UNIQUE_DIMENSIONS) for s in specs}
    assert len(combos) == 12
    assert len({s["taxonomy"]["domain"] for s in specs}) == 7  # every domain used before any repeats twice
    assert sample_specs("b-x", 12) == specs


# --- the happy path ----------------------------------------------------------


def test_accepts_in_band_and_publishes(svc):
    svc.submit_batch(1, "b-1")
    settle(svc, job_id(T, 1, "generate"), status="LEASED")
    svc.reconcile()
    assert (task(svc)["status"], task(svc)["stage"]) == ("CALIBRATING", "GENERATING")

    settle(svc, job_id(T, 1, "generate"), outcome="OK", bundle_sha256="sha1", result={"name": "queue-drain-bug"})
    (svc.artifacts.bundle_path("sha1")).mkdir(parents=True)
    (svc.artifacts.bundle_path("sha1") / "instruction.md").write_text("fix it\n")
    svc.reconcile()
    assert task(svc)["stage"] == "VALIDATING" and task(svc)["name"] == "queue-drain-bug"
    val = svc.store.job(job_id(T, 1, "validate", 0))
    assert val["input"]["bundle_dir"].endswith("bundles/sha1")

    settle(svc, val["id"], outcome="OK", result={"gates": {}})
    svc.reconcile()
    assert task(svc)["stage"] == "EVALUATING"
    rollouts = [svc.store.job(rollout_job_id(T, 1, s, 0)) for s in range(1, 6)]
    assert all(r and r["input"]["task_dir"].endswith("bundles/sha1") and r["config_hash"] for r in rollouts)

    score(svc, 1, "01010")
    t = task(svc)
    assert (t["status"], t["stage"], t["accepted_version"]) == ("ACCEPTED", "BAND_OK", 1)
    published = svc.accepted_dir / "queue-drain-bug"
    assert (published / "instruction.md").read_text() == "fix it\n"
    text = (published / "verdict.md").read_text()
    assert "ACCEPTED: v1 passed 2/5 valid rollouts (band 1–3)" in text
    assert "run 1 missed the retry path" in text
    assert svc.store.unhandled() == []  # every settled job was acted on


def test_publish_is_retried_after_a_crash_and_never_holds_the_lock(svc, monkeypatch):
    svc.submit_batch(1, "b-1")
    build(svc, 1, "sha1")
    real_publish = svc.artifacts.publish

    def crash(*args):
        # The ACCEPTED transaction has committed and released the write lock.
        assert task(svc)["status"] == "ACCEPTED"
        with svc.store.tx():
            pass
        raise RuntimeError("killed mid-copy")

    monkeypatch.setattr(svc.artifacts, "publish", crash)
    with pytest.raises(RuntimeError):
        score(svc, 1, "10000")
    assert not (svc.accepted_dir / "queue-drain-bug").exists()

    monkeypatch.setattr(svc.artifacts, "publish", real_publish)
    svc.reconcile()
    assert "ACCEPTED: v1 passed 1/5" in (svc.accepted_dir / "queue-drain-bug" / "verdict.md").read_text()
    svc.reconcile()  # already published: nothing is copied again
    assert published(svc) == ["queue-drain-bug"]


def test_two_accepted_tasks_with_one_name_both_publish(svc):
    svc.submit_batch(2, "b-1")
    for tid in ("b-1-t01", "b-1-t02"):
        sha = f"sha-{tid}"
        svc.artifacts.bundle_path(sha).mkdir(parents=True)
        settle(svc, job_id(tid, 1, "generate"), outcome="OK", bundle_sha256=sha, result={"name": "same-name"})
        svc.reconcile()
        settle(svc, job_id(tid, 1, "validate", 0), outcome="OK", result={})
        svc.reconcile()
        for slot, mark in enumerate("10000", start=1):
            settle(svc, rollout_job_id(tid, 1, slot, 0), outcome="VALID_PASS" if mark == "1" else "VALID_FAIL")
        svc.reconcile()
    assert published(svc) == ["same-name", "same-name-b-1-t02"]


def test_reconcile_is_a_no_op_when_nothing_changed(svc):
    svc.submit_batch(2, "b-1")
    before = [svc.store.task_jobs(i) for i in ("b-1-t01", "b-1-t02")]
    assert svc.reconcile() == 0  # nothing claimed yet: tasks stay CREATED / QUEUED
    assert (task(svc)["status"], task(svc)["stage"]) == ("CREATED", "QUEUED")
    assert svc.reconcile() == 0
    assert [svc.store.task_jobs(i) for i in ("b-1-t01", "b-1-t02")] == before


# --- repairs -------------------------------------------------------------------


def test_failed_gate_gets_repaired_then_rejected(svc):
    svc.submit_batch(1, "b-1")
    settle(svc, job_id(T, 1, "generate"), outcome="OK", bundle_sha256="sha1", result={})
    svc.reconcile()
    for k in (0, 1, 2):
        settle(svc, job_id(T, 1, "validate", k), outcome="FAILED", result={"feedback": f"oracle failed ({k})"})
        svc.reconcile()
        if k < 2:
            repair = svc.store.job(job_id(T, 1, "repair", k + 1))
            assert repair["input"] == {"origin": "repair", "bundle_sha256": f"sha{k + 1}",
                                       "feedback": f"oracle failed ({k})"}
            assert task(svc)["stage"] == "GENERATING"
            settle(svc, repair["id"], outcome="OK", bundle_sha256=f"sha{k + 2}", result={})
            svc.reconcile()
            assert svc.store.job(job_id(T, 1, "validate", k + 1))["bundle_sha256"] == f"sha{k + 2}"
    t = task(svc)
    assert (t["status"], t["reason"], t["stage"]) == ("REJECTED", "invalid", "INVALID")


def test_failed_generate_rejects_invalid_and_dead_rejects_infra(svc):
    svc.submit_batch(2, "b-1")
    settle(svc, job_id("b-1-t01", 1, "generate"), outcome="FAILED", error="no acceptable draft")
    settle(svc, job_id("b-1-t02", 1, "generate"), status="DEAD")
    svc.reconcile()
    assert task(svc, "b-1-t01")["reason"] == "invalid"
    assert task(svc, "b-1-t02")["reason"] == "infra_unresolved"


# --- calibration -----------------------------------------------------------------


def test_too_easy_is_revised_until_exhausted(svc):
    svc.submit_batch(1, "b-1")
    build(svc, 1, "sha1")
    score(svc, 1, "11111")
    t = task(svc)
    assert (t["status"], t["current_version"], t["stage"]) == ("CALIBRATING", 2, "QUEUED")
    rev = svc.store.job(job_id(T, 2, "generate"))
    assert rev["input"]["origin"] == "revision" and rev["input"]["bundle_sha256"] == "sha1"
    assert rev["input"]["operator"] == "increase_diagnosis_depth"
    assert rev["input"]["result"] == "5 of 5 runs passed."

    build(svc, 2, "sha2")
    score(svc, 2, "11110")
    assert svc.store.job(job_id(T, 3, "generate"))["input"]["operator"] == "increase_constraint_interaction"

    build(svc, 3, "sha3")
    score(svc, 3, "11111")
    t = task(svc)
    assert (t["status"], t["reason"], t["current_version"]) == ("REJECTED", "exhausted", 3)
    assert not svc.accepted_dir.exists()


def test_too_hard_is_made_easier_then_accepted_at_v2(svc):
    svc.submit_batch(1, "b-1")
    build(svc, 1, "sha1")
    score(svc, 1, "00000")
    rev = svc.store.job(job_id(T, 2, "generate"))
    assert rev["input"]["operator"] == "localize_diagnosis"
    assert "run 3 missed the retry path" in rev["input"]["evidence"]
    build(svc, 2, "sha2", name="queue-drain-bug")
    score(svc, 2, "00100")
    t = task(svc)
    assert (t["status"], t["accepted_version"]) == ("ACCEPTED", 2)
    assert "ACCEPTED: v2 passed 1/5" in (svc.accepted_dir / "queue-drain-bug" / "verdict.md").read_text()


def test_revision_evidence_from_executor_rows_without_a_trace_parser(svc):
    """The executor writes result.trial / duration_sec but no `failure`; the
    failing tests come from the verifier output in its trace dir."""
    svc.submit_batch(1, "b-1")
    build(svc, 1, "sha1")
    for slot in range(1, 6):
        rid = rollout_job_id(T, 1, slot, 0)
        stdout = svc.artifacts.trace_dir(rid) / "trial-x" / "verifier" / "test-stdout.txt"
        stdout.parent.mkdir(parents=True)
        stdout.write_text(
            "PASSED tests/test_outputs.py::test_happy_path\n"
            f"FAILED tests/test_outputs.py::test_crash_recovery - AssertionError: lost job {slot}\n"
        )
        settle(svc, rid, outcome="VALID_FAIL", reward=0.0, result={"trial": "trial-x", "duration_sec": 120.4})
    svc.reconcile()
    evidence = svc.store.job(job_id(T, 2, "generate"))["input"]["evidence"]
    assert evidence.splitlines()[0] == (
        "- run 1: failed, 120s: tests/test_outputs.py::test_crash_recovery - AssertionError: lost job 1"
    )


# --- invalid rollouts ------------------------------------------------------------


def test_invalid_rollouts_are_retried_then_unresolved(svc):
    svc.submit_batch(1, "b-1")
    build(svc, 1, "sha1")
    for retry in range(4):
        settle(svc, rollout_job_id(T, 1, 1, retry), outcome="INVALID", error="AgentTimeoutError")
        svc.reconcile()
        if retry < 3:
            assert svc.store.job(rollout_job_id(T, 1, 1, retry + 1))["status"] == "READY"
            assert task(svc)["stage"] == "EVALUATING"
    t = task(svc)
    assert (t["status"], t["reason"], t["stage"]) == ("REJECTED", "infra_unresolved", "UNRESOLVED")


def test_rejecting_a_task_cancels_its_open_jobs(svc):
    """Slot 1 is out of retries while slots 2–5 are still queued or running:
    the task is rejected and the rest of its work is cancelled, not left orphaned."""
    svc.submit_batch(1, "b-1")
    build(svc, 1, "sha1")
    settle(svc, rollout_job_id(T, 1, 2, 0), status="LEASED")  # a worker is mid-trial
    with svc.store.tx() as conn:
        for retry in (1, 2, 3):
            insert_job(conn, id=rollout_job_id(T, 1, 1, retry), task_id=T, version=1,
                       type=JobType.ROLLOUT, slot=1, retry=retry)
    for retry in range(4):
        settle(svc, rollout_job_id(T, 1, 1, retry), outcome="INVALID", error="AgentTimeoutError")
    svc.reconcile()
    assert (task(svc)["status"], task(svc)["reason"]) == ("REJECTED", "infra_unresolved")
    for slot in (2, 3, 4, 5):
        j = svc.store.job(rollout_job_id(T, 1, slot, 0))
        assert (j["status"], j["error"]) == ("DEAD", "cancelled: task REJECTED (infra_unresolved)")
    assert svc.store.open_job_counts() == {"ready": 0, "leased": 0, "expired": 0}
    assert svc.store.unhandled() == []


def test_retry_already_created_by_executor_is_not_duplicated(svc):
    svc.submit_batch(1, "b-1")
    build(svc, 1, "sha1")
    settle(svc, rollout_job_id(T, 1, 2, 0), status="DEAD")
    svc.reconcile()
    svc.reconcile()
    assert job_ids(svc).count(rollout_job_id(T, 1, 2, 1)) == 1
    # A scored retry counts for its slot; the dead attempt never does.
    settle(svc, rollout_job_id(T, 1, 2, 1), outcome="VALID_PASS", reward=1.0)
    for slot in (1, 3, 4, 5):
        settle(svc, rollout_job_id(T, 1, slot, 0), outcome="VALID_FAIL", reward=0.0)
    svc.reconcile()
    assert (task(svc)["status"], task(svc)["accepted_version"]) == ("ACCEPTED", 1)


# --- concurrency -------------------------------------------------------------------


def test_two_job_services_racing_take_each_step_once(backend, tmp_path):
    a = JobService(backend, accepted_dir=tmp_path / "accepted")
    b = JobService(from_env({"SDE_WORK_DIR": str(tmp_path / "work")}), accepted_dir=tmp_path / "accepted")
    b.queue = RecordingQueue()
    a.submit_batch(6, "b-1")
    for i in range(1, 7):
        settle(a, job_id(f"b-1-t{i:02d}", 1, "generate"), outcome="OK", bundle_sha256=f"sha{i}", result={})

    def spin(svc):
        for _ in range(5):
            svc.reconcile()

    threads = [threading.Thread(target=spin, args=(s,)) for s in (a, b, a, b)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    ids = [j["id"] for i in range(1, 7) for j in a.store.task_jobs(f"b-1-t{i:02d}")]
    assert len(ids) == len(set(ids)) == 12  # one generate + one validate per task
    published = backend.queue.published + b.queue.published
    validates = [i for i in published if "-validate-" in i]
    assert sorted(validates) == sorted(set(validates)) and len(validates) == 6
