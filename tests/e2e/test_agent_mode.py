"""
Agent mode end to end (plan v2, INT): every task starts from a world.

    POST /v1/tasks ─► world ─► world_check ─fail─► world-repair (≤2) ─► REJECTED invalid
                                   │ ok
                                   ▼
                     create (from the world) ─► validate ─► rollout ×5 ─► analyze
                        too easy → world mutation (v+1, parent = this world) ─► world_check ─► create
                        too hard → revise (easier) on the same world

Same processes and backends as test_e2e.py; the world author, the world check, the
task author and the gates are the fakes in fakes.py (no model, no Docker).
"""

import json

import pytest

from shared.contracts import models
from shared.storage.sql import job_id

AGENT_SCENARIO = {
    "t01": {"versions": {"1": "11111", "2": "01100"}},   # too easy → the world is mutated → 2/5 at v2
    "t02": {"world_failures": 3},                        # the world never checks out → 2 repairs → invalid
    "t03": {"world_failures": 1, "versions": {"1": "00100"}},  # world repaired once, then 1/5 at v1
    "t04": {"versions": {"1": "00000", "2": "01000"}},   # too hard → task revised on the same world → v2
    "t05": {"create_leak": 1, "versions": {"1": "01000"}},  # create's leak review fails once → repaired → v1
}

EXPECTED = {  # task -> (status, reason, accepted_version)
    "t01": ("ACCEPTED", None, 2),
    "t02": ("REJECTED", "invalid", None),
    "t03": ("ACCEPTED", None, 1),
    "t04": ("ACCEPTED", None, 2),
    "t05": ("ACCEPTED", None, 1),
}


@pytest.fixture
def agent_pipeline(make_pipeline, monkeypatch):
    """A pipeline whose API (in this process) and stack (subprocesses) run in agent mode."""
    monkeypatch.setattr(models, "AUTHOR_MODE", "agent")  # the in-process API's submit reads it per call
    p = make_pipeline(AGENT_SCENARIO)
    p.env["SDE_AUTHOR_MODE"] = "agent"
    return p


def _jobs(p, suffix):
    return {j["id"]: j for j in p.backend.store.task_jobs(f"e2e-{suffix}")}


def _world_of(jobs, tid, version, k=0):
    kind, n = ("world", 0) if k == 0 else ("world-repair", k)
    return jobs[job_id(tid, version, kind, n) if k else job_id(tid, version, kind)]["result"]["world_sha256"]


def test_agent_mode_runs_worlds_to_the_scripted_verdicts(agent_pipeline):
    p = agent_pipeline
    p.submit()
    first = {s: p.backend.store.task_jobs(f"e2e-{s}")[0] for s in AGENT_SCENARIO}
    assert all(j["type"] == "world" and j["input"]["origin"] == "initial" for j in first.values())
    assert all({"family", "shape", "dial"} <= set(j["input"]["spec"]) for j in first.values())  # a v2 spec
    p.wait(p.start())

    got = {s: p.backend.store.task(f"e2e-{s}") for s in AGENT_SCENARIO}
    for s, (status, reason, accepted) in EXPECTED.items():
        assert (got[s]["status"], got[s]["reason"], got[s]["accepted_version"]) == (status, reason, accepted), s

    # Nothing was interrupted, so every paid unit ran exactly once.
    for kind in ("world", "generate", "trial"):
        assert all(e == {"start": 1, "finish": 1} for e in p.events(kind).values()), kind
    assert set(p.events("world_check")) >= {job_id("e2e-t02", 1, "world-check", k) for k in range(3)}


def test_too_easy_mutates_the_world_not_the_task(agent_pipeline):
    p = agent_pipeline
    p.submit()
    p.wait(p.start())
    tid, jobs = "e2e-t01", _jobs(p, "t01")
    v1_world = _world_of(jobs, tid, 1)
    assert jobs[job_id(tid, 1, "create")]["input"]["world_sha256"] == v1_world

    mutation = jobs[job_id(tid, 2, "world")]
    inp = mutation["input"]
    assert inp["origin"] == "mutation" and inp["parent_world_sha256"] == v1_world
    assert inp["mutation"]["operators"] and len(inp["mutations"]) == 1
    assert inp["mutations"][0]["rules"] and inp["mutations"][0]["pass_count"] == 5
    assert {"shape", "dial"} <= set(inp["spec"])
    # The new world records its lineage; the v2 task was written from it, after its own check.
    v2_world = mutation["result"]["world_sha256"]
    # SQLite mode keeps worlds in the machine's work dir; in cluster mode S3 has them too.
    doc = next(w / "worlds" / v2_world / "world.json" for w in p.work_dirs
               if (w / "worlds" / v2_world / "world.json").is_file())
    lineage = json.loads(doc.read_text())["lineage"]
    assert lineage["parent_world_sha256"] == v1_world and lineage["mutations"] == inp["mutations"]
    assert jobs[job_id(tid, 2, "world-check", 0)]["outcome"] == "OK"
    assert jobs[job_id(tid, 2, "create")]["input"]["world_sha256"] == v2_world
    assert not any(j["type"] == "revise" for j in jobs.values())  # harder = a new world, not a reworded task

    decision = next(j for j in jobs.values() if j["type"] == "analyze" and j["version"] == 1
                    and (j["result"] or {}).get("action") == "revise")
    assert decision["result"]["mutation"]["operators"] == inp["mutation"]["operators"]

    # The task view and the verdict say v2 is a mutation, with its operators (not the
    # "initial" of the create that followed the world check).
    v2 = p.api("GET", f"/v1/tasks/{tid}")["versions"][1]
    assert (v2["origin"], v2["mutation_operators"]) == ("mutation", inp["mutation"]["operators"])
    from evaluation import verdict

    text = verdict.render(p.backend.store.task(tid), list(jobs.values()), "ACCEPTED", 5)
    row = next(line for line in text.splitlines() if line.startswith("| 2 |"))
    assert "| mutation |" in row and inp["mutation"]["operators"][0] in row


def test_a_world_that_never_checks_out_is_repaired_then_rejected(agent_pipeline):
    p = agent_pipeline
    p.submit()
    p.wait(p.start())
    tid, jobs = "e2e-t02", _jobs(p, "t02")
    assert [jobs[job_id(tid, 1, "world-check", k)]["outcome"] for k in range(3)] == ["FAILED"] * 3
    for k in (1, 2):
        repair = jobs[job_id(tid, 1, "world-repair", k)]
        assert repair["input"]["origin"] == "repair" and "doesn't reproduce" in repair["input"]["feedback"]
        assert repair["input"]["world_sha256"] == _world_of(jobs, tid, 1, k - 1)
    assert not any(j["type"] in ("create", "validate", "rollout") for j in jobs.values())

    # One repair was enough for t03: its task came from the repaired world.
    tid3, jobs3 = "e2e-t03", _jobs(p, "t03")
    assert jobs3[job_id(tid3, 1, "create")]["input"]["world_sha256"] == _world_of(jobs3, tid3, 1, 1)


def test_a_leaking_draft_is_repaired_from_its_world_not_rejected(agent_pipeline):
    p = agent_pipeline
    p.submit()
    p.wait(p.start())
    tid, jobs = "e2e-t05", _jobs(p, "t05")
    world = _world_of(jobs, tid, 1)
    create = jobs[job_id(tid, 1, "create")]
    assert create["outcome"] == "FAILED" and create["result"]["failure_kind"] == "author"
    repair = jobs[job_id(tid, 1, "repair", 1)]
    assert repair["input"]["origin"] == "repair" and repair["input"]["world_sha256"] == world
    assert repair["input"]["bundle_sha256"] == create["result"]["bundle_sha256"]
    assert "leak" in repair["input"]["feedback"] and repair["outcome"] == "OK"
    assert jobs[job_id(tid, 1, "validate", 1)]["outcome"] == "OK"


def test_the_real_agent_adapters_across_two_machines(make_pipeline, monkeypatch):
    """generator/service.py's world_fn / draft_fn and validator/service.py's world_check_fn for real, on two machines
    sharing S3: every world, bundle and draft record a job needs was written on whichever
    machine ran the job before it, so the adapters must fetch it (the fakes assert it is
    local before they start) and register what they wrote. Only the authoring trial and
    Docker are faked."""
    monkeypatch.setattr(models, "AUTHOR_MODE", "agent")
    p = make_pipeline({"t01": {"versions": {"1": "11111", "2": "01100"}},  # mutation → v2
                       "t02": {"versions": {"1": "00000", "2": "01000"}},  # revise(easier) → v2
                       "t03": {"versions": {"1": "00100"}}})
    if p.kind != "cluster":
        pytest.skip("machines share artifacts only through S3")
    p.env.update({"SDE_AUTHOR_MODE": "agent", "SDE_E2E_REAL_ADAPTERS": "1"})
    p.submit()
    p.wait(p.start("m1"), p.start("m2"))

    got = {s: p.backend.store.task(f"e2e-{s}") for s in ("t01", "t02", "t03")}
    assert [(t["status"], t["accepted_version"]) for t in got.values()] == [("ACCEPTED", 2)] * 2 + [("ACCEPTED", 1)]
    jobs = [j for s in got for j in p.backend.store.task_jobs(f"e2e-{s}")]
    agent = [j for j in jobs if j["type"] in ("world", "create", "revise")]
    # The real adapters mapped every result and saved every authoring trial.
    assert all(j["outcome"] == "OK" and j["trace_uri"] for j in agent), [j["id"] for j in agent]
    checks = [j for j in jobs if j["type"] == "world_check"]
    assert checks and all(j["outcome"] == "OK" and j["result"]["checks"]["repro_fails"]["passed"]
                          and j["result"]["world_sha256"] for j in checks)
    # Work really did cross machines, so the fetches above were real.
    assert len(p.pids("generate") | p.pids("world")) == 2


def test_too_hard_revises_the_task_on_the_same_world(agent_pipeline):
    p = agent_pipeline
    p.submit()
    p.wait(p.start())
    tid, jobs = "e2e-t04", _jobs(p, "t04")
    world = _world_of(jobs, tid, 1)
    revise = jobs[job_id(tid, 2, "revise")]
    assert revise["input"]["origin"] == "easier" and revise["input"]["world_sha256"] == world
    assert revise["result"]["world_sha256"] == world
    assert not any(j["type"] == "world" and j["version"] == 2 for j in jobs.values())

    verdict = (p.accepted / "e2e-t04" / "verdict.md")
    if verdict.exists():  # published under the task id
        assert f"`{world[:12]}`" in verdict.read_text()
