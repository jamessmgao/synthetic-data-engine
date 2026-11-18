import json

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from shared.storage import from_env
from shared.storage.sql import insert_job, job_id, rollout_job_id
from shared.contracts.models import JobType

KEY = "test-key"
AUTH = {"X-Api-Key": KEY}
GOOD_GATES = {g: {"passed": True} for g in ("structural", "lint", "oracle1", "oracle2", "nop")}


@pytest.fixture
def backend(tmp_path):
    return from_env({"SDE_WORK_DIR": str(tmp_path / "work")})


@pytest.fixture
def client(backend):
    return TestClient(create_app(backend, api_key=KEY))


def add_job(backend, task_id, version, type_, id, status="DONE", **cols):
    """Write a settled job row the way a worker would."""
    slot, retry = cols.pop("slot", 0), cols.pop("retry", 0)
    job_input = cols.pop("input", None)
    with backend.store.tx() as conn:
        insert_job(conn, id=id, task_id=task_id, version=version, type=JobType(type_), slot=slot, retry=retry,
                   input=job_input)
        cols = {k: json.dumps(v) if isinstance(v, (dict, list)) else v for k, v in cols.items()}
        sets = "".join(f", {k} = ?" for k in cols)
        conn.execute(f"UPDATE jobs SET status = ?, finished_at = created_at{sets} WHERE id = ?",
                     (status, *cols.values(), id))


def rollout(backend, task_id, version, slot, outcome, retry=0, **cols):
    reward = {"VALID_PASS": 1.0, "VALID_FAIL": 0.0}.get(outcome)
    add_job(backend, task_id, version, "rollout", rollout_job_id(task_id, version, slot, retry), slot=slot,
            retry=retry, outcome=outcome, reward=reward, config_hash="cfg1", **cols)


def set_task(backend, task_id, **fields):
    with backend.store.tx() as conn:
        conn.execute(f"UPDATE tasks SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?",
                     (*fields.values(), task_id))


# --- auth and health --------------------------------------------------------


def test_requires_api_key(client):
    assert client.post("/v1/tasks", json={}).status_code == 401
    assert client.post("/v1/tasks", json={}, headers={"X-Api-Key": "wrong"}).status_code == 401


def test_closed_without_configured_key(backend):
    client = TestClient(create_app(backend, api_key=""))
    assert client.get("/v1/tasks/x", headers={"X-Api-Key": ""}).status_code == 503


def test_healthz_needs_no_key(client):
    body = client.get("/healthz").json()
    assert body["status"] == "ok" and body["backends"]["store"] == "sqlite"


# --- task creation (one task per call) ---------------------------------------


def create(client, **body):
    r = client.post("/v1/tasks", json=body, headers=AUTH)
    assert r.status_code == 202, r.text
    return r.json()


def test_empty_request_samples_a_spec_and_queues_one_create_job(client, backend):
    body = create(client)
    assert body["created"] is True and body["task_id"].startswith("t-")
    job = backend.store.job(job_id(body["task_id"], 1, "create"))
    assert job["status"] == "READY" and job["input"]["origin"] == "initial"
    assert job["input"]["spec"]["taxonomy"]["domain"] and job["input"]["spec"]["difficulty"]


def test_a_text_prompt_seeds_the_spec(client, backend):
    body = create(client, prompt="A Go job queue that loses jobs when a worker crashes.")
    spec = backend.store.job(job_id(body["task_id"], 1, "create"))["input"]["spec"]
    assert spec["seed"] == "A Go job queue that loses jobs when a worker crashes."
    assert spec["difficulty"] == {"form": "behavior_preserving", "dial": 2} and "taxonomy" not in spec


def test_an_explicit_spec_is_passed_through_with_design(client, backend):
    spec = {"taxonomy": {"domain": "databases", "environment": "Python service"},
            "design": ["Hidden test: a TLC counterexample trace must be replayed."], "extra_key": 1}
    body = create(client, spec=spec, task_id="t-explicit")
    got = backend.store.job(job_id("t-explicit", 1, "create"))["input"]["spec"]
    assert body == {"task_id": "t-explicit", "created": True}
    assert got["taxonomy"] == spec["taxonomy"] and got["design"] == spec["design"] and got["extra_key"] == 1
    assert got["difficulty"]["form"] == "behavior_preserving"  # missing difficulty gets the default


def test_a_raw_prompt_replaces_the_template_and_is_flagged(client, backend):
    raw = "You write one hard Rust task about lock-free queues. ...the whole user message..."
    body = create(client, raw_prompt=raw, task_id="t-raw")
    job = backend.store.job(job_id("t-raw", 1, "create"))
    # The generator sends input.prompt verbatim as the user message (no template, no spec).
    assert job["input"] == {"origin": "initial", "prompt": raw}
    assert backend.store.task("t-raw")["slot"] == {"raw_prompt": raw} and body["created"]
    assert create(client, raw_prompt=raw, task_id="t-raw")["created"] is False


def test_sampled_tasks_spread_across_the_taxonomy(client, backend):
    ids = [create(client)["task_id"] for _ in range(5)]
    combos = {tuple(backend.store.task(t)["slot"]["taxonomy"][d] for d in ("domain", "task_type", "environment"))
              for t in ids}
    assert len(combos) == 5


def test_a_retried_task_id_is_idempotent_and_409_on_a_different_request(client, backend):
    assert create(client, prompt="idea one", task_id="t-1") == {"task_id": "t-1", "created": True}
    assert create(client, prompt="idea one", task_id="t-1") == {"task_id": "t-1", "created": False}
    assert create(client, task_id="t-1")["created"] is False  # no request body to compare: just returns it
    r = client.post("/v1/tasks", json={"prompt": "idea two", "task_id": "t-1"}, headers=AUTH)
    assert r.status_code == 409
    assert len(backend.store.tasks()) == 1


@pytest.mark.parametrize("body", [
    {"prompt": ""}, {"prompt": "x", "spec": {"taxonomy": {"domain": "d"}}}, {"task_id": "Bad/Id"},
    {"spec": {}}, {"spec": {"design": ["x"] * 41}}, {"prompt": 3},
    {"raw_prompt": ""}, {"raw_prompt": "x", "prompt": "y"}, {"raw_prompt": "x", "spec": {"taxonomy": {"d": "v"}}},
])
def test_creation_validation_is_422(client, body):
    assert client.post("/v1/tasks", json=body, headers=AUTH).status_code == 422


# --- list, verdict, report --------------------------------------------------


def test_unknown_ids_are_404(client):
    assert client.get("/v1/tasks/nope", headers=AUTH).status_code == 404
    assert client.get("/v1/report", params={"task_id": "nope"}, headers=AUTH).status_code == 404


@pytest.fixture
def worked_batch(client, backend):
    """Three tasks a client created one by one (see work_three)."""
    return work_three(backend, *(create(client, task_id=f"t-0{i}")["task_id"] for i in (1, 2, 3)))


def work_three(backend, t1, t2, t3):
    """t1: v1 too easy (5/5), v2 accepted at 2/5. t2: rejected invalid. t3: mid-evaluation."""
    add_job(backend, t1, 1, "validate", job_id(t1, 1, "validate", 0), outcome="OK", result={"gates": GOOD_GATES})
    for slot in range(1, 6):
        rollout(backend, t1, 1, slot, "VALID_PASS", cost_usd=0.01)
    add_job(backend, t1, 2, "revise", job_id(t1, 2, "revise"), outcome="OK", bundle_sha256="sha-v2",
            cost_usd=0.5, input={"origin": "revision", "operator": "increase_diagnosis_depth"})
    add_job(backend, t1, 2, "validate", job_id(t1, 2, "validate", 0), outcome="OK", result={"gates": GOOD_GATES})
    # A timed-out rollout retried in the same slot.
    rollout(backend, t1, 2, 1, "INVALID", retry=0, error="AgentTimeoutError")
    rollout(backend, t1, 2, 1, "VALID_FAIL", retry=1,
            failure={"explanation": "missed the retry path", "attribution": "model"})
    for slot, outcome in zip(range(2, 6), ("VALID_PASS", "VALID_FAIL", "VALID_PASS", "VALID_FAIL")):
        rollout(backend, t1, 2, slot, outcome)
    set_task(backend, t1, status="ACCEPTED", current_version=2, stage="BAND_OK", accepted_version=2)

    failed = {**GOOD_GATES, "oracle1": {"passed": False, "reward": 0.0}}
    add_job(backend, t2, 1, "validate", job_id(t2, 1, "validate", 0), outcome="FAILED", result={"gates": failed})
    set_task(backend, t2, status="REJECTED", reason="invalid", stage="INVALID")

    add_job(backend, t3, 1, "validate", job_id(t3, 1, "validate", 0), outcome="OK", result={"gates": GOOD_GATES})
    rollout(backend, t3, 1, 2, "VALID_PASS")
    set_task(backend, t3, status="CALIBRATING", stage="EVALUATING")
    return [t1, t2, t3]


def test_task_list(client, worked_batch):
    by_id = {t["id"]: t for t in client.get("/v1/tasks", headers=AUTH).json()}
    assert set(by_id) == {"t-01", "t-02", "t-03"}
    assert by_id["t-01"]["pass_pattern"] == "✗✓✗✓✗"
    assert by_id["t-03"]["pass_pattern"] == "·✓···"
    assert by_id["t-02"]["reason"] == "invalid"
    accepted = client.get("/v1/tasks", params={"status": "ACCEPTED"}, headers=AUTH).json()
    assert [t["id"] for t in accepted] == ["t-01"]


def test_task_verdict(client, worked_batch):
    body = client.get("/v1/tasks/t-01", headers=AUTH).json()
    assert body["decision"] == "ACCEPTED: v2 passed 2/5 valid rollouts (band 1–3)"
    assert body["task"]["slot"]["taxonomy"]["domain"]
    v1, v2 = body["versions"]
    assert (v1["state"], v1["pass_count"], v1["origin"]) == ("OUT_OF_BAND", 5, "initial")
    assert (v2["state"], v2["pass_count"], v2["operator"]) == ("BAND_OK", 2, "increase_diagnosis_depth")
    assert v2["bundle_sha256"] == "sha-v2" and v2["gates"] == GOOD_GATES
    # Every rollout is shown, including the invalid one that was retried.
    slot1 = [r for r in body["rollouts"] if r["version"] == 2 and r["slot"] == 1]
    assert [(r["retry"], r["outcome"], r["error"]) for r in slot1] == [
        (0, "INVALID", "AgentTimeoutError"),
        (1, "VALID_FAIL", None),
    ]
    assert slot1[1]["failure"]["attribution"] == "model"

    pending = client.get("/v1/tasks/t-03", headers=AUTH).json()
    assert pending["decision"] == "PENDING: v1 is EVALUATING"
    assert pending["versions"][0]["pass_count"] is None


def test_task_verdict_shows_the_analysis_behind_each_revision(client, backend):
    tid = create(client, task_id="t-easy")["task_id"]
    for slot in range(1, 6):
        # files_modified is a list of paths, as the Trace Parser writes it (aws-iter1-t01: a count crashed this)
        rollout(backend, tid, 1, slot, "VALID_PASS",
                trace_metrics={"llm_calls": 10 + slot, "files_modified": ["/app/engine.py"]})
    add_job(backend, tid, 1, "analyze", f"{tid}-v1-analyze-wait", outcome="OK",
            result={"action": "wait", "summary": "4/5 runs scored"})
    review = {"why_easy": "no test checks that evaluation stays incremental", "simple_strategy": "clear all caches",
              "fix_applied": "rewrote engine.py", "cues": [{"quote": "A name may be defined more than once.",
                                                           "where": "instruction"}],
              "suggestions": [{"technique": "genuine_tradeoff", "change": "bound the recomputation",
                               "defeats": "recompute-all"}],
              "cited_evidence": ["E4"], "trusted": True, "runs_reviewed": ["run 1"]}
    mutation = {"version": 2, "from_version": 1, "pass_count": 5, "direction": "harder",
                "operators": ["remove_cues", "add_persistent_state"],
                "why": ["the instruction points at the fix", "the passing fix kept a guard in one process"],
                "rules": [{"operator": "remove_cues", "rule": "cues_found", "evidence": "pass review cues"},
                          {"operator": "add_persistent_state", "rule": "process_local_guard",
                           "evidence": 'pass review said "cache"'}],
                "target_fix": "rewrote engine.py"}
    add_job(backend, tid, 1, "analyze", f"{tid}-v1-analyze", outcome="OK", cost_usd=0.2,
            result={"action": "revise", "summary": "5/5 passed; revising (harder)", "pass_count": 5,
                    "direction": "harder", "operator": "increase_diagnosis_depth",
                    "easy_analysis": review, "mutation": mutation})
    add_job(backend, tid, 2, "world", job_id(tid, 2, "world"), status="LEASED",
            input={"origin": "mutation", "mutation": {"operators": mutation["operators"]}})
    rollout(backend, tid, 2, 1, "VALID_PASS")
    set_task(backend, tid, status="CALIBRATING", current_version=2, stage="WORLD_BUILDING")

    v1, v2 = client.get(f"/v1/tasks/{tid}", headers=AUTH).json()["versions"]
    a = v1["analysis"]
    assert (a["job_id"], a["action"], a["direction"]) == (f"{tid}-v1-analyze", "revise", "harder")
    assert a["mutation"]["rules"][1] == {"operator": "add_persistent_state", "rule": "process_local_guard",
                                         "evidence": 'pass review said "cache"'}
    assert a["mutation"]["why"] == mutation["why"] and a["mutation"]["target_fix"] == "rewrote engine.py"
    assert a["review"]["why_easy"] == review["why_easy"] and a["review"]["trusted"] is True
    assert a["review"]["suggestions"][0]["technique"] == "genuine_tradeoff"
    assert "cited_evidence" not in a["review"]
    assert a["signals"] == {"passing_runs": 5, "files_modified_median": 1, "steps_median": 13, "cues": 1}
    assert v2["mutation_operators"] == ["remove_cues", "add_persistent_state"]
    assert v2["analysis"] is None  # not decided yet


def test_report_over_named_tasks(client, worked_batch):
    r = client.get("/v1/report", params={"task_id": worked_batch}, headers=AUTH).json()
    assert r["task_ids"] == ["t-01", "t-02", "t-03"] and r["candidates"] == 3
    assert r["structural_pass"] == 3 and r["functional_pass"] == 2
    assert (r["accepted_v1"], r["accepted_total"]) == (0, 1)
    assert r["rejected"] == {"invalid": 1} and r["in_progress"] == 1
    assert r["pass_count_histogram"] == {"0": 0, "1": 0, "2": 1, "3": 0, "4": 0, "5": 1}
    assert (r["valid_rollouts"], r["invalid_rollouts"]) == (11, 1)
    assert r["cost_usd"] == {"authoring": 0.5, "evaluation": 0.05, "total": 0.55, "per_accepted": 0.55,
                             "authoring_per_task": 0.166667, "evaluation_per_task": 0.016667}
    assert r["config_hash"] == "cfg1"
    assert r["wall_clock_s"] is not None
    # One finished candidate of two accepted; t-03 is still running.
    assert r["yield"] == {"candidates": 3, "functional": 2, "accepted": 1, "accepted_v1": 0,
                          "learnable_rate": 0.5}
    # A subset, and everything created since a time, use the same report.
    assert client.get("/v1/report", params={"task_id": ["t-02"]}, headers=AUTH).json()["candidates"] == 1
    assert client.get("/v1/report", params={"since": "2000-01-01"}, headers=AUTH).json()["candidates"] == 3


def test_fresh_report_has_nulls_not_zeros(client):
    ids = [create(client)["task_id"] for _ in range(2)]
    r = client.get("/v1/report", params={"task_id": ids}, headers=AUTH).json()
    assert r["cost_usd"] == {"authoring": None, "evaluation": None, "total": None, "per_accepted": None,
                             "authoring_per_task": None, "evaluation_per_task": None}
    assert r["wall_clock_s"] is None and r["config_hash"] is None
    assert r["yield"]["learnable_rate"] is None  # nothing has finished


# --- batches: N tasks under one label, reported together ---------------------


def create_batch(client, **body):
    r = client.post("/v1/batches", json=body, headers=AUTH)
    assert r.status_code == 202, r.text
    return r.json()


def test_batch_creates_tasks_and_their_create_jobs(client, backend):
    body = create_batch(client, count=3, batch_id="b-1")
    assert body == {"batch_id": "b-1", "task_ids": ["b-1-t01", "b-1-t02", "b-1-t03"], "created": True}
    for tid in body["task_ids"]:
        task = backend.store.task(tid)
        assert task["batch_id"] == "b-1" and task["status"] == "CREATED"
        (job,) = backend.store.task_jobs(tid)
        assert job["id"] == f"{tid}-v1-create" and job["input"]["spec"] == task["slot"]


def test_batch_specs_are_diverse_within_the_batch(client, backend):
    create(client, task_id="solo")
    ids = create_batch(client, count=6)["task_ids"]
    combos = {tuple(backend.store.task(t)["slot"]["taxonomy"][d] for d in ("domain", "task_type", "environment"))
              for t in ["solo", *ids]}
    assert len(combos) == 7


def test_batch_prompt_applies_to_every_task(client, backend):
    ids = create_batch(client, count=2, prompt="a flaky retry loop")["task_ids"]
    assert {backend.store.task(t)["slot"]["seed"] for t in ids} == {"a flaky retry loop"}


def test_batch_is_idempotent_and_409_on_conflict(client, backend):
    first = create_batch(client, count=2, batch_id="b-1")
    again = create_batch(client, count=2, batch_id="b-1")
    assert again == {**first, "created": False}
    assert client.post("/v1/batches", json={"count": 3, "batch_id": "b-1"}, headers=AUTH).status_code == 409
    assert client.post("/v1/batches", json={"count": 2, "batch_id": "b-1", "prompt": "x"},
                       headers=AUTH).status_code == 409
    assert len(backend.store.tasks(batch_id="b-1")) == 2
    # A task id the batch would use, taken by a single task, is a conflict too.
    create(client, task_id="b-2-t01")
    assert client.post("/v1/batches", json={"count": 1, "batch_id": "b-2"}, headers=AUTH).status_code == 409


def test_batch_defaults_to_ten_with_a_generated_id(client):
    body = create_batch(client)
    assert body["batch_id"].startswith("b-") and len(body["task_ids"]) == 10


@pytest.mark.parametrize("body", [
    {"count": 0}, {"count": 51}, {"count": "x"}, {"batch_id": "Bad/Id"},
    {"prompt": "x", "spec": {"seed": "y"}}, {"spec": {"difficulty": {"dial": 1}}},
])
def test_batch_validation_is_422(client, body):
    assert client.post("/v1/batches", json=body, headers=AUTH).status_code == 422


def test_batch_budget_is_stored_reported_and_part_of_the_request(client, backend):
    create_batch(client, count=2, batch_id="b-1", budget_usd=0.5)
    row = backend.store.conn.execute("SELECT budget_usd FROM batches WHERE id = 'b-1'").fetchone()
    assert row["budget_usd"] == 0.5
    add_job(backend, "b-1-t01", 1, "rollout", "b-1-t01-paid", slot=9, cost_usd=0.5)
    report = client.get("/v1/batches/b-1/report", headers=AUTH).json()
    assert report["budget"] == {"cap_usd": 0.5, "spent_usd": 0.5, "leased_estimate_usd": 0.0,
                                "remaining_usd": 0.0, "budget_exhausted": True}
    assert report["pipeline_budget"]["cap_usd"] is None
    # The same batch again with another budget is a different request.
    assert client.post("/v1/batches", json={"count": 2, "batch_id": "b-1", "budget_usd": 5},
                       headers=AUTH).status_code == 409
    assert create_batch(client, count=2, batch_id="b-1", budget_usd=0.5)["created"] is False
    assert client.post("/v1/batches", json={"count": 1, "budget_usd": -1}, headers=AUTH).status_code == 422


def test_report_shows_the_instruction_size_per_authoring_step(client, backend):
    create(client, task_id="t1")

    def authored(type_, id, tokens, origin=None):
        add_job(backend, "t1", 1, type_, id, input={"origin": origin} if origin else None,
                result={"authoring": {"instruction_size": {"chars": tokens * 4, "tokens_est": tokens}}})

    authored("world", "t1-v1-world", 2000, "initial")
    authored("world", "t1-v1-world-repair1", 2200, "repair")
    authored("world", "t1-v2-world", 1400, "mutation")
    authored("create", "t1-v1-create-x", 4000)
    authored("create", "t1-v2-create-x", 4400)
    add_job(backend, "t1", 1, "validate", "t1-v1-validate-r0", result={"gates": {}})  # not an authoring trial
    got = client.get("/v1/report", params={"task_id": "t1"}, headers=AUTH).json()["instruction_tokens"]
    assert got == {"create": {"n": 2, "mean": 4200, "max": 4400}, "world": {"n": 1, "mean": 2000, "max": 2000},
                   "world_mutation": {"n": 1, "mean": 1400, "max": 1400},
                   "world_repair": {"n": 1, "mean": 2200, "max": 2200}}


def test_health_and_report_show_the_pipeline_budget(client, backend, monkeypatch):
    create(client, task_id="t1")
    add_job(backend, "t1", 1, "rollout", "t1-paid", slot=9, cost_usd=2.0)
    monkeypatch.delenv("SDE_BUDGET_USD", raising=False)
    health = client.get("/healthz").json()
    assert health["status"] == "ok" and health["budget"]["cap_usd"] is None
    monkeypatch.setenv("SDE_BUDGET_USD", "2")
    health = client.get("/healthz").json()
    assert health["status"] == "budget_exhausted" and health["budget"]["spent_usd"] == 2.0
    report = client.get("/v1/report", headers=AUTH).json()
    assert report["budget"]["budget_exhausted"] is True and report["budget"]["remaining_usd"] == 0.0


def test_unknown_batch_is_404(client):
    assert client.get("/v1/batches/nope", headers=AUTH).status_code == 404
    assert client.get("/v1/batches/nope/report", headers=AUTH).status_code == 404


@pytest.fixture
def worked_real_batch(client, backend):
    create(client, task_id="solo")  # not in the batch: must not leak into its numbers
    return work_three(backend, *create_batch(client, count=3, batch_id="b-1")["task_ids"])


def test_batch_list(client, worked_real_batch):
    create_batch(client, count=1, batch_id="b-2")
    first, second = client.get("/v1/batches", headers=AUTH).json()
    assert first["batch_id"] == "b-2"  # newest first
    assert {k: second[k] for k in ("batch_id", "tasks", "accepted", "rejected", "in_progress")} == {
        "batch_id": "b-1", "tasks": 3, "accepted": 1, "rejected": 1, "in_progress": 1}


def test_batch_status(client, worked_real_batch):
    body = client.get("/v1/batches/b-1", headers=AUTH).json()
    assert body["counts_by_status"] == {"ACCEPTED": 1, "REJECTED": 1, "CALIBRATING": 1}
    assert [t["id"] for t in body["tasks"]] == worked_real_batch
    assert body["tasks"][0]["pass_pattern"] == "✗✓✗✓✗"


def test_cancelling_a_batch_stops_only_its_undecided_tasks(client, backend, worked_real_batch):
    t1, t2, t3 = worked_real_batch  # accepted, rejected, mid-evaluation
    add_job(backend, t3, 1, "rollout", "b-1-t03-waiting", status="READY", slot=3)
    r = client.post("/v1/batches/b-1/cancel", json={"note": "wrong area"}, headers=AUTH)
    assert r.status_code == 200
    # t3's open jobs: its create from the submit (READY in this fixture) and the waiting rollout.
    assert r.json() == {"batch_id": "b-1", "cancelled": 1, "already_decided": 2, "closed_jobs": 2,
                        "tasks": {t1: "ACCEPTED", t2: "REJECTED", t3: "CANCELLED"}}
    again = client.post("/v1/batches/b-1/cancel", headers=AUTH).json()  # idempotent, no body needed
    assert again["cancelled"] == 0 and again["closed_jobs"] == 0 and again["already_decided"] == 3
    assert backend.store.task("solo")["status"] == "CREATED"  # outside the batch: untouched

    report = client.get("/v1/batches/b-1/report", headers=AUTH).json()
    assert report["cancelled"] == 1 and report["in_progress"] == 0
    assert report["rejected"] == {"invalid": 1}  # not counted as a failure
    assert report["yield"]["learnable_rate"] == 0.5  # 1 accepted of 2 finished; the cancel isn't a candidate
    [summary] = [b for b in client.get("/v1/batches", headers=AUTH).json() if b["batch_id"] == "b-1"]
    assert (summary["cancelled"], summary["in_progress"]) == (1, 0)
    task = client.get(f"/v1/tasks/{t3}", headers=AUTH).json()
    assert task["decision"].startswith("CANCELLED by user at ") and task["decision"].endswith(": wrong area")


def test_cancelling_one_task(client):
    create(client, task_id="t1")
    r = client.post("/v1/tasks/t1/cancel", json={"by": "operator"}, headers=AUTH)
    assert r.status_code == 200 and r.json()["cancelled"] == 1 and r.json()["closed_jobs"] == 1  # its create
    assert client.get("/v1/tasks/t1", headers=AUTH).json()["decision"].startswith("CANCELLED by operator at ")
    assert client.post("/v1/tasks/nope/cancel", headers=AUTH).status_code == 404
    assert client.post("/v1/batches/nope/cancel", headers=AUTH).status_code == 404
    assert client.post("/v1/tasks/t1/cancel").status_code == 401


def test_batch_report_matches_the_task_report(client, worked_real_batch):
    r = client.get("/v1/batches/b-1/report", headers=AUTH).json()
    assert r["batch_id"] == "b-1" and r["task_ids"] == worked_real_batch
    assert r["yield"] == {"candidates": 3, "functional": 2, "accepted": 1, "accepted_v1": 0,
                          "learnable_rate": 0.5}
    named = client.get("/v1/report", params={"task_id": worked_real_batch}, headers=AUTH).json()
    # The same summary; only the budget is per batch (plus the pipeline's) there.
    per_batch = ("batch_id", "budget", "pipeline_budget")
    assert {k: v for k, v in r.items() if k not in per_batch} == {k: v for k, v in named.items() if k != "budget"}
    assert r["pipeline_budget"] == named["budget"]
