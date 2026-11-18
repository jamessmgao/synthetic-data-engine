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

SLOTS_SCRIPT = """
import json, os, socket, sys
sys.path.insert(0, sys.argv[1])
from shared.storage import from_env
from shared.storage.sql import insert_job, utcnow
from shared.jobs.jobqueue import JobQueue
from shared.contracts.models import DOCKER_JOB_TYPES, JobType
b = from_env(); jq = JobQueue(b)
with b.store.tx() as c:
    now = utcnow()
    c.execute("INSERT INTO tasks (id, status, stage, created_at, updated_at) VALUES ('t', 'CALIBRATING', 'QUEUED', ?, ?)", (now, now))
    insert_job(c, id="t-world", task_id="t", version=1, type=JobType.WORLD)
    for i in range(2):
        insert_job(c, id=f"t-create-{i}", task_id="t", version=1, type=JobType.CREATE)
    insert_job(c, id="t-rollout", task_id="t", version=1, type=JobType.ROLLOUT, slot=1)
owner = lambda i: f"{socket.gethostname()}:{os.getpid()}:{i}"
got = [jq.claim(["create", "world"], owner(i), docker_slots=2) for i in range(3)]
rollout = jq.claim(["rollout"], owner(9), docker_slots=2)
print(json.dumps({"docker_types": list(map(str, DOCKER_JOB_TYPES)),
                  "authoring": sum(j is not None for j in got), "rollout": rollout is not None}))
"""


@pytest.mark.parametrize("mode,authoring,rollout", [("agent", 2, False), ("oneshot", 3, True)])
def test_agent_jobs_hold_a_docker_slot(tmp_path, mode, authoring, rollout):
    env = {**os.environ, "SDE_AUTHOR_MODE": mode, "SDE_WORK_DIR": str(tmp_path / "work")}
    for var in ("DATABASE_URL", "REDIS_URL", "S3_BUCKET"):
        env.pop(var, None)
    out = subprocess.run([sys.executable, "-c", SLOTS_SCRIPT, str(REPO)], env=env,
                         capture_output=True, text=True, check=True).stdout
    got = json.loads(out)
    assert {"world", "create", "revise"} <= set(got["docker_types"]) if mode == "agent" \
        else not {"world", "create", "revise"} & set(got["docker_types"])
    assert "world_check" in got["docker_types"]  # a gate in every mode
    # agent: 2 slots, both taken by authoring trials, so the 3rd and the rollout wait.
    assert got["authoring"] == authoring and got["rollout"] is rollout


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


def test_a_world_that_could_not_be_built_rejects(backend, jq):
    t = add_task(backend)
    with backend.store.tx() as c:
        insert_job(c, id=f"{t}-v1-world", task_id=t, version=1, type=JobType.WORLD, input={"spec": {}})
    jq.publish([f"{t}-v1-world"])
    run_job(backend, jq, "world", {"outcome": "FAILED", "error": "agent gave up"}, handoffs.after_world(backend))
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
