"""INF1: agent-mode authoring in the pipeline (docs/AGENTIC_AUTHORING_PLAN.md v2).

- In agent mode world / create / revise jobs hold a Docker slot, shared per machine
  with the gates (validate, world_check) and rollouts.
- Hand-offs: world → world_check → create → (validate, as before); a failed check
  repairs the world (≤ MAX_REPAIRS_PER_VERSION), then rejects the task.
- Adapters call run_world_job / run_create_job / run_world_check_job (stubbed here:
  TB1-3 provide the real ones), move worlds, bundles and drafts between machines
  through the artifact store, and save each agent trial so a reclaim adopts it.
"""

import json
import os
import subprocess
import sys
import types
import uuid
from pathlib import Path

import pytest
from tests.shared.conftest import S3, add_task

from shared.jobs import handoffs, worker
from shared.storage import Backend
from shared.storage.artifacts import S3ArtifactStore
from shared.storage.sql import insert_job
from shared.jobs.jobqueue import JobQueue
from shared.contracts.models import MAX_REPAIRS_PER_VERSION, JobType

REPO = Path(__file__).resolve().parents[3]

def _jobs(backend, jq, t, specs):
    ids = []
    with backend.store.tx() as c:
        for jid, type, inp in specs:
            insert_job(c, id=f"{t}-{jid}", task_id=t, version=1, type=type, input=inp)
            ids.append(f"{t}-{jid}")
    jq.publish(ids)


def _owner(i, host="runner-a"):
    return f"{host}:{os.getpid()}:{i}"


@pytest.fixture
def on_host(monkeypatch):
    import shared.jobs.jobqueue as jqmod
    monkeypatch.setattr(jqmod.socket, "gethostname", lambda: "runner-a")


def test_slots_are_decided_per_job(backend, jq, on_host):
    t = add_task(backend)
    _jobs(backend, jq, t, [("oneshot", JobType.CREATE, {"origin": "initial", "spec": {}}),
                           ("agent", JobType.CREATE, {"origin": "initial", "world_sha256": "w"}),
                           ("world", JobType.WORLD, {"spec": {}})])
    rows = {j["id"].rsplit("-", 1)[1]: j["needs_docker"] for j in backend.store.task_jobs(t)}
    assert rows == {"oneshot": 0, "agent": 1, "world": 1}


def test_one_shot_drafts_never_wait_for_a_docker_slot(backend, jq, on_host):
    t = add_task(backend)
    _jobs(backend, jq, t, [("r1", JobType.ROLLOUT, {}), ("c1", JobType.CREATE, {"origin": "initial"}),
                           ("c2", JobType.CREATE, {"origin": "initial"})])
    assert jq.claim("rollout", _owner(0), docker_slots=1) is not None  # the only slot is taken
    got = [jq.claim(["create", "world"], _owner(i), docker_slots=1, agent_slots=1) for i in (1, 2)]
    assert all(g is not None and g["needs_docker"] == 0 for g in got)


def test_agent_jobs_leave_slots_for_the_gates_and_rollouts(backend, jq, on_host):
    t = add_task(backend)
    _jobs(backend, jq, t, [(f"w{i}", JobType.WORLD, {"spec": {}}) for i in range(3)]
          + [("c", JobType.CREATE, {"world_sha256": "w"}), ("v", JobType.VALIDATE, {})])
    gen = [jq.claim(["create", "world"], _owner(i), docker_slots=3, agent_slots=2) for i in range(4)]
    assert sum(g is not None for g in gen) == 2  # SDE_AGENT_SLOTS of the 3 slots
    assert jq.claim("validate", _owner(9), docker_slots=3) is not None  # the third is free for gates
    assert jq.claim("validate", _owner(10), docker_slots=3) is None


def test_a_full_machine_skips_the_claim_transactions(backend, jq, on_host, monkeypatch):
    import shared.jobs.jobqueue as jqmod
    t = add_task(backend)
    _jobs(backend, jq, t, [("r1", JobType.ROLLOUT, {}), ("r2", JobType.ROLLOUT, {}), ("r3", JobType.ROLLOUT, {})])
    assert jq.claim("rollout", _owner(0), docker_slots=1)
    tries = []
    real = jqmod.sql_claim
    monkeypatch.setattr(jqmod, "sql_claim", lambda *a, **kw: tries.append(1) or real(*a, **kw))
    for i in range(5):
        assert jq.claim("rollout", _owner(i + 1), docker_slots=1) is None
    assert tries == []  # one slot count per poll, no locked claim attempts


def test_agent_slots_default_leaves_one_slot_free():
    p = subprocess.run([sys.executable, "-c", "import sys; sys.path.insert(0, sys.argv[1]); "
                        "from shared.contracts.models import AGENT_SLOTS; print(AGENT_SLOTS)", str(REPO)],
                       env={**os.environ, "SDE_DOCKER_SLOTS": "8"}, capture_output=True, text=True, check=True)
    assert p.stdout.strip() == "7"


def test_unknown_author_mode_is_refused():
    p = subprocess.run([sys.executable, "-c", "import sys; sys.path.insert(0, sys.argv[1]); import shared.contracts.models",
                        str(REPO)], env={**os.environ, "SDE_AUTHOR_MODE": "agnet"}, capture_output=True, text=True)
    assert p.returncode != 0 and "SDE_AUTHOR_MODE" in p.stderr


# --- the world hand-off chain ---------------------------------------------------------


def run_job(backend, jq, job_type, fields, then):
    job = jq.claim(job_type, "w")
    assert job is not None, f"no {job_type} job ready"
    assert jq.settle(job["id"], job["lease_token"], fields, then=then)
    return job


def jobs_of(backend, task_id):
    return {j["id"]: j for j in backend.store.task_jobs(task_id)}


def stage(backend, task_id):
    return backend.store.task(task_id)["stage"], backend.store.task(task_id)["status"]


def test_world_check_create_chain_with_one_world_repair(backend, jq):
    t = add_task(backend)
    spec = {"family": "database_consistency", "shape": "fix", "dial": 2}
    with backend.store.tx() as c:
        insert_job(c, id=f"{t}-v1-world", task_id=t, version=1, type=JobType.WORLD,
                   input={"origin": "initial", "spec": spec})
    jq.publish([f"{t}-v1-world"])

    run_job(backend, jq, "world", {"outcome": "OK", "result": {"world_sha256": "w1"}},
            handoffs.after_world(backend))
    check = jobs_of(backend, t)[f"{t}-v1-world-check-r0"]
    assert check["type"] == "world_check" and check["input"]["world_sha256"] == "w1"
    assert check["input"]["spec"] == spec and stage(backend, t)[0] == "WORLD_CHECKING"

    run_job(backend, jq, "world_check", {"outcome": "FAILED", "result": {"feedback": "repro passes"}},
            handoffs.after_world_check(backend))
    repair = jobs_of(backend, t)[f"{t}-v1-world-repair1"]
    assert repair["type"] == "world" and stage(backend, t)[0] == "WORLD_BUILDING"
    assert repair["input"] == {"origin": "repair", "world_sha256": "w1", "feedback": "repro passes", "spec": spec}

    run_job(backend, jq, "world", {"outcome": "OK", "result": {"world_sha256": "w2"}},
            handoffs.after_world(backend))
    assert f"{t}-v1-world-check-r1" in jobs_of(backend, t)
    run_job(backend, jq, "world_check", {"outcome": "OK", "result": {}}, handoffs.after_world_check(backend))
    create = jobs_of(backend, t)[f"{t}-v1-create"]
    assert create["type"] == "create" and stage(backend, t)[0] == "QUEUED"
    assert create["input"] == {"origin": "initial", "world_sha256": "w2", "shape": "fix", "dial": 2}


def test_world_check_failing_past_the_repair_budget_rejects(backend, jq):
    t = add_task(backend)
    with backend.store.tx() as c:
        insert_job(c, id=f"{t}-v1-world", task_id=t, version=1, type=JobType.WORLD, input={"spec": {}})
    jq.publish([f"{t}-v1-world"])
    for k in range(MAX_REPAIRS_PER_VERSION + 1):
        run_job(backend, jq, "world", {"outcome": "OK", "result": {"world_sha256": f"w{k}"}},
                handoffs.after_world(backend))
        run_job(backend, jq, "world_check", {"outcome": "FAILED", "error": "fix doesn't hold"},
                handoffs.after_world_check(backend))
    assert stage(backend, t) == ("INVALID", "REJECTED")
    assert jq.claim(["world", "world_check", "create"], "w") is None


def test_a_failed_world_run_is_retried_then_rejected(backend, jq):
    t = add_task(backend)
    inp = {"origin": "initial", "spec": {"family": "database_consistency"}}
    with backend.store.tx() as c:
        insert_job(c, id=f"{t}-v1-world", task_id=t, version=1, type=JobType.WORLD, input=inp)
    jq.publish([f"{t}-v1-world"])
    for r in range(1, MAX_REPAIRS_PER_VERSION + 1):
        run_job(backend, jq, "world", {"outcome": "FAILED", "error": "AgentTimeoutError",
                                       "result": {"failure_kind": "infra"}}, handoffs.after_world(backend))
        retry = jobs_of(backend, t)[f"{t}-v1-world-retry{r}"]
        assert retry["type"] == "world" and retry["input"] == inp and stage(backend, t)[0] == "WORLD_BUILDING"
    run_job(backend, jq, "world", {"outcome": "FAILED", "result": {"failure_kind": "author"}},
            handoffs.after_world(backend))
    assert stage(backend, t) == ("INVALID", "REJECTED")
    assert f"{t}-v1-world-retry{MAX_REPAIRS_PER_VERSION + 1}" not in jobs_of(backend, t)


def test_a_retried_world_repair_keeps_its_check_round(backend, jq):
    t = add_task(backend)
    with backend.store.tx() as c:
        insert_job(c, id=f"{t}-v1-world-repair1", task_id=t, version=1, type=JobType.WORLD,
                   input={"origin": "repair", "world_sha256": "w0", "spec": {}})
    jq.publish([f"{t}-v1-world-repair1"])
    run_job(backend, jq, "world", {"outcome": "FAILED", "result": {"failure_kind": "infra"}},
            handoffs.after_world(backend))
    run_job(backend, jq, "world", {"outcome": "OK", "result": {"world_sha256": "w1"}}, handoffs.after_world(backend))
    assert f"{t}-v1-world-repair1-retry1" in jobs_of(backend, t)
    assert jobs_of(backend, t)[f"{t}-v1-world-check-r1"]["input"]["round"] == 1


def test_ok_without_a_world_sha_rejects_instead_of_hanging(backend, jq):
    t = add_task(backend)
    with backend.store.tx() as c:
        insert_job(c, id=f"{t}-v1-world", task_id=t, version=1, type=JobType.WORLD, input={"spec": {}})
    jq.publish([f"{t}-v1-world"])
    run_job(backend, jq, "world", {"outcome": "OK", "result": {}}, handoffs.after_world(backend))
    assert stage(backend, t) == ("INVALID", "REJECTED")


# --- adapters, against stub authoring functions -------------------------------------


@pytest.fixture
def authoring(monkeypatch):
    """Stubs for run_world_job / run_create_job / run_world_check_job that write what
    the real ones write (plan §3.3-3.5) and record what they found locally."""
    calls: list = []

    def trial(work, job_id):
        d = Path(work) / "traces" / job_id / "trial"
        already = (d / "result.json").is_file()
        d.mkdir(parents=True, exist_ok=True)
        (d / "result.json").write_text("{}")
        return already

    def run_world_job(job_id, job_input, work_dir=None):
        work = Path(work_dir)
        parent = job_input.get("parent_world_sha256") or job_input.get("world_sha256")
        sha = uuid.uuid4().hex
        w = work / "worlds" / sha
        (w / "environment").mkdir(parents=True)
        (w / "repro").mkdir()
        (w / "repro" / "repro.sh").write_text("exit 1\n")
        (w / "world.json").write_text(json.dumps({"invariants": ["x"], "lineage": {"parent_world_sha256": parent}}))
        calls.append({"fn": "world", "job_id": job_id,
                      "parent_local": bool(parent) and (work / "worlds" / parent / "world.json").is_file(),
                      "trial_adopted": trial(work, job_id)})
        return {"outcome": "OK", "error": None, "cost_usd": 0.3,
                "result": {"world_sha256": sha, "world_path": str(w), "name": "w", "authoring": {}}}

    def run_create_job(job_id, job_input, work_dir=None):
        work = Path(work_dir)
        world = job_input["world_sha256"]
        sha = uuid.uuid4().hex
        b = work / "bundles" / sha
        (b / "environment").mkdir(parents=True)
        (b / "instruction.md").write_text(f"task from {job_id}\n")
        (work / "drafts" / f"{sha}.json").write_text(json.dumps({"draft": {"name": "t"}}))
        calls.append({"fn": "create", "job_id": job_id,
                      "world_local": (work / "worlds" / world / "world.json").is_file(),
                      "trial_adopted": trial(work, job_id)})
        return {"outcome": "OK", "error": None, "cost_usd": 0.5,
                "result": {"name": "t", "bundle_sha256": sha, "bundle_path": str(b), "world_sha256": world,
                           "draft_path": str(work / "drafts" / f"{sha}.json"), "llm_calls": []}}

    def run_world_check_job(world_dir, trace_dir):
        calls.append({"fn": "world_check", "world_ok": (Path(world_dir) / "world.json").is_file()})
        Path(trace_dir).mkdir(parents=True, exist_ok=True)
        return {"outcome": "OK", "failed_check": None, "feedback": "", "cost_usd": None,
                "result": {"checks": {"build": True, "repro_fails": True, "repro_passes_with_fix": True}}}

    monkeypatch.setitem(sys.modules, "author", types.SimpleNamespace(
        run_world_job=run_world_job, run_create_job=run_create_job))
    monkeypatch.setitem(sys.modules, "gates", types.SimpleNamespace(
        run_world_check_job=run_world_check_job, run_validate_job=None))
    return calls


def test_agent_adapters_register_and_settle(backend, authoring):
    t = add_task(backend)
    fns = worker.generation_fns(backend, mode="agent")
    w = fns["world"]({"id": f"{t}-v1-world", "type": "world", "input": {"origin": "initial", "spec": {}}})
    world = w["result"]["world_sha256"]
    assert w["outcome"] == "OK" and w["cost_usd"] == 0.3 and w["trace_uri"]
    chk = worker.gate_fns(backend, mode="agent")["world_check"](
        {"id": f"{t}-v1-world-check-r0", "type": "world_check", "input": {"world_sha256": world}})
    assert chk["outcome"] == "OK" and chk["result"]["checks"]["repro_fails"] and chk["result"]["world_sha256"] == world
    c = fns["create"]({"id": f"{t}-v1-create", "type": "create", "bundle_sha256": None,
                       "input": {"origin": "initial", "world_sha256": world, "shape": "fix", "dial": 2}})
    assert c["bundle_sha256"] and c["result"]["world_sha256"] == world and c["trace_uri"]


@pytest.mark.skipif(not S3, reason="needs the compose S3 (SDE_TEST_S3_ENDPOINT)")
def test_worlds_drafts_and_trials_follow_the_job_across_machines(tmp_path, backend, authoring):
    bucket = f"sde-test-{uuid.uuid4().hex[:8]}"

    def machine(name):
        return Backend(store=backend.store, queue=backend.queue,
                       artifacts=S3ArtifactStore(tmp_path / name, bucket, S3), work_dir=tmp_path / name)

    m1, m2, m3, m4 = (machine(f"m{i}") for i in range(1, 5))
    t = add_task(backend)
    world = worker.world_fn(m1)({"id": f"{t}-v1-world", "input": {"spec": {}}})["result"]["world_sha256"]
    # The world check runs on another runner: it downloads the world.
    worker.world_check_fn(m2)({"id": f"{t}-v1-world-check-r0", "input": {"world_sha256": world}})
    assert authoring[-1] == {"fn": "world_check", "world_ok": True}
    # So does the create job, on a third.
    bundle = worker.draft_fn(m3, "agent")({"id": f"{t}-v1-create", "bundle_sha256": None,
                                          "input": {"origin": "initial", "world_sha256": world}})["bundle_sha256"]
    assert authoring[-1]["world_local"] is True
    # A mutation on a fourth gets the parent world; a repair of the task gets the bundle + draft.
    worker.world_fn(m4)({"id": f"{t}-v2-world", "input": {"origin": "mutation", "parent_world_sha256": world}})
    assert authoring[-1]["parent_local"] is True
    worker.draft_fn(m1, "agent")({"id": f"{t}-v1-repair1", "bundle_sha256": None,
                                  "input": {"origin": "repair", "bundle_sha256": bundle, "world_sha256": world}})
    assert (m1.work_dir / "drafts" / f"{bundle}.json").is_file()  # m3 wrote it; m1 fetched it
    assert (m1.work_dir / "bundles" / bundle / "instruction.md").is_file()
    # A machine that reclaims a finished agent job adopts its saved trial.
    worker.world_fn(m2)({"id": f"{t}-v2-world", "input": {"origin": "mutation", "parent_world_sha256": world}})
    assert authoring[-1]["trial_adopted"] is True


def test_agent_mode_picks_the_draft_function_per_job(backend, authoring, monkeypatch):
    """Raw prompts, v1 specs and tasks in flight when the mode switched have no world:
    they run one-shot even in agent mode; only jobs from a world run the authoring trial."""
    oneshot = []

    def run_generate_job(job_id, job_input, work_dir=None):
        oneshot.append(job_id)
        return {"outcome": "FAILED", "error": "no draft", "cost_usd": 0.01, "result": {}}

    monkeypatch.setitem(sys.modules, "worker", types.SimpleNamespace(run_generate_job=run_generate_job))
    t = add_task(backend)
    draft = worker.draft_fn(backend, "agent")
    fields = draft({"id": f"{t}-v1-create", "bundle_sha256": None, "input": {"origin": "initial", "prompt": "x"}})
    assert oneshot == [f"{t}-v1-create"] and "trace_uri" not in fields and not authoring
    revised = draft({"id": f"{t}-v2-revise", "bundle_sha256": None,
                     "input": {"origin": "revision", "operator": "increase_diagnosis_depth"}})
    assert oneshot[-1] == f"{t}-v2-revise" and revised["outcome"] == "FAILED"

    world = worker.world_fn(backend)({"id": f"{t}-w", "input": {"spec": {}}})["result"]["world_sha256"]
    fields = draft({"id": f"{t}-v3-create", "bundle_sha256": None,
                    "input": {"origin": "initial", "world_sha256": world}})
    assert authoring[-1]["fn"] == "create" and fields["result"]["world_sha256"] == world
    assert len(oneshot) == 2  # the world job did not go one-shot


def test_a_world_task_keeps_its_world_through_validate_and_repair(backend):
    """The repair of a task written from a world must also run from that world."""
    from shared.jobs import handoffs

    t = add_task(backend)
    with backend.store.tx() as c:
        c.execute("UPDATE tasks SET stage = 'GENERATING' WHERE id = ?", (t,))
        [validate] = handoffs.after_generation(backend)(c, {
            "id": f"{t}-v1-create", "task_id": t, "version": 1, "outcome": "OK", "bundle_sha256": "b1",
            "input": {"origin": "initial", "world_sha256": "w1"}, "result": {"world_sha256": "w1"}})
    assert validate["input"]["world_sha256"] == "w1"
    with backend.store.tx() as c:
        [repair] = handoffs.after_validate(backend)(c, {
            "id": validate["id"], "task_id": t, "version": 1, "outcome": "FAILED", "bundle_sha256": "b1",
            "input": validate["input"], "result": {"feedback": "the tests accept the plausible wrong fix"},
            "error": None})
    assert repair["input"]["origin"] == "repair" and repair["input"]["world_sha256"] == "w1"


def test_a_trial_is_saved_even_when_the_authoring_function_raises(backend, monkeypatch):
    """A raising run_world_job releases the job; the retry (maybe elsewhere) must find the
    partial trial instead of starting over and paying again."""
    saved = []

    def run_world_job(job_id, job_input, work_dir=None):
        trial = Path(work_dir) / "traces" / job_id / "authoring" / "t" / "agent.log"
        trial.parent.mkdir(parents=True)
        trial.write_text("40 minutes of work")
        raise RuntimeError("docker daemon went away")

    monkeypatch.setitem(sys.modules, "author", types.SimpleNamespace(run_world_job=run_world_job,
                                                                     run_create_job=None))
    monkeypatch.setattr(backend.artifacts, "save_trace", lambda jid: saved.append(jid) or f"traces/{jid}/")
    with pytest.raises(RuntimeError):
        worker.world_fn(backend)({"id": "t-v1-world", "input": {"spec": {}}})
    assert saved == ["t-v1-world"]


def test_the_create_jobs_leak_review_is_shared_with_validate(backend, monkeypatch, authoring):
    registered, fetched = [], []
    monkeypatch.setattr(backend.artifacts, "register_file", lambda rel: registered.append(rel))
    monkeypatch.setattr(backend.artifacts, "fetch_file", lambda rel: fetched.append(rel) or False)
    real_create = sys.modules["author"].run_create_job

    def create_with_review(job_id, job_input, work_dir=None):
        out = real_create(job_id, job_input, work_dir)
        review = Path(work_dir) / "leak_reviews" / out["result"]["bundle_sha256"] / "review.json"
        review.parent.mkdir(parents=True)
        review.write_text("{}")
        return out

    monkeypatch.setattr(sys.modules["author"], "run_create_job", create_with_review)
    world = worker.world_fn(backend)({"id": "t-v1-world", "input": {"spec": {}}})["result"]["world_sha256"]
    sha = worker.draft_fn(backend, "agent")({"id": "t-v1-create", "bundle_sha256": None,
                                             "input": {"origin": "initial", "world_sha256": world}})["bundle_sha256"]
    assert f"leak_reviews/{sha}/review.json" in registered
    monkeypatch.setitem(sys.modules, "gates", types.SimpleNamespace(
        run_validate_job=lambda b, t: {"outcome": "OK", "result": {"gates": {}}, "feedback": ""},
        run_world_check_job=None))
    worker.validate_fn(backend)({"id": "t-v1-validate-r0", "bundle_sha256": sha, "input": {}})
    assert f"leak_reviews/{sha}/review.json" in fetched


def _leaking_create(t: str, sha: str = "b1") -> dict:
    """A create job's settle when the leak review (plan N6) rejected the draft it wrote."""
    return {"outcome": "FAILED", "error": "instruction leaks the fix", "bundle_sha256": sha,
            "result": {"failure_kind": "author", "bundle_sha256": sha, "world_sha256": "w1",
                       "feedback": "A reviewer ... found sentences that give away the root cause"}}


def test_a_leaking_create_is_repaired_then_validated(backend, jq):
    t = add_task(backend)
    with backend.store.tx() as c:
        c.execute("UPDATE tasks SET stage = 'QUEUED' WHERE id = ?", (t,))
        insert_job(c, id=f"{t}-v1-create", task_id=t, version=1, type=JobType.CREATE,
                   input={"origin": "initial", "world_sha256": "w1", "shape": "fix", "dial": 2})
    jq.publish([f"{t}-v1-create"])

    run_job(backend, jq, "create", _leaking_create(t), handoffs.after_generation(backend))
    repair = jobs_of(backend, t)[f"{t}-v1-repair1"]
    assert repair["type"] == "revise" and repair["input"] == {
        "origin": "repair", "bundle_sha256": "b1", "world_sha256": "w1",
        "feedback": "A reviewer ... found sentences that give away the root cause"}
    assert stage(backend, t) == ("GENERATING", "CALIBRATING")  # not rejected

    run_job(backend, jq, "revise", {"outcome": "OK", "bundle_sha256": "b2", "result": {"world_sha256": "w1"}},
            handoffs.after_generation(backend))
    validate = jobs_of(backend, t)[f"{t}-v1-validate-r1"]
    assert validate["input"]["bundle_sha256"] == "b2" and validate["input"]["world_sha256"] == "w1"
    assert stage(backend, t)[0] == "BUILDING"


@pytest.mark.parametrize("fields", [
    {"outcome": "FAILED", "error": "no draft", "result": {"failure_kind": "author"}},  # nothing to repair
    {"outcome": "FAILED", "error": "AgentTimeoutError", "result": {"failure_kind": "infra", "bundle_sha256": "b1"}},
    {"outcome": "FAILED", "error": "no draft", "result": {}},  # one-shot failure, as before
])
def test_create_failures_without_a_repairable_draft_still_reject(backend, jq, fields):
    t = add_task(backend)
    with backend.store.tx() as c:
        c.execute("UPDATE tasks SET stage = 'QUEUED' WHERE id = ?", (t,))
        insert_job(c, id=f"{t}-v1-create", task_id=t, version=1, type=JobType.CREATE, input={"origin": "initial"})
    jq.publish([f"{t}-v1-create"])
    run_job(backend, jq, "create", fields, handoffs.after_generation(backend))
    assert stage(backend, t) == ("INVALID", "REJECTED")
    assert not any("repair" in j for j in jobs_of(backend, t))


def test_a_leaking_repair_past_the_budget_rejects(backend, jq):
    t = add_task(backend)
    last = f"{t}-v1-repair{MAX_REPAIRS_PER_VERSION}"
    with backend.store.tx() as c:
        c.execute("UPDATE tasks SET stage = 'GENERATING' WHERE id = ?", (t,))
        insert_job(c, id=last, task_id=t, version=1, type=JobType.REVISE,
                   input={"origin": "repair", "bundle_sha256": "b0", "feedback": "x"})
    jq.publish([last])
    run_job(backend, jq, "revise", _leaking_create(t), handoffs.after_generation(backend))
    assert stage(backend, t) == ("INVALID", "REJECTED")


def test_oneshot_create_is_unchanged(backend, monkeypatch):
    calls = []

    def run_generate_job(job_id, job_input, work_dir=None):
        calls.append(job_id)
        return {"outcome": "FAILED", "error": "no draft", "cost_usd": 0.01, "result": {}}

    monkeypatch.setitem(sys.modules, "worker", types.SimpleNamespace(run_generate_job=run_generate_job))
    fns = worker.generation_fns(backend)
    assert set(fns) == {"create", "revise"}
    fields = fns["create"]({"id": "x-v1-create", "type": "create", "bundle_sha256": None,
                            "input": {"origin": "initial"}})
    assert calls == ["x-v1-create"] and fields["outcome"] == "FAILED" and "trace_uri" not in fields
