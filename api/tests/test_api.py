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


# --- task creation (one task per call; no batches) -------------------------


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
    """Three tasks a client created one by one. t01: v1 too easy (5/5), v2 accepted at
    2/5. t02: rejected invalid. t03: mid-evaluation."""
    t1, t2, t3 = (create(client, task_id=f"t-0{i}")["task_id"] for i in (1, 2, 3))

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


def test_report_over_named_tasks(client, worked_batch):
    r = client.get("/v1/report", params={"task_id": worked_batch}, headers=AUTH).json()
    assert r["task_ids"] == ["t-01", "t-02", "t-03"] and r["candidates"] == 3
    assert r["structural_pass"] == 3 and r["functional_pass"] == 2
    assert (r["accepted_v1"], r["accepted_total"]) == (0, 1)
    assert r["rejected"] == {"invalid": 1} and r["in_progress"] == 1
    assert r["pass_count_histogram"] == {"0": 0, "1": 0, "2": 1, "3": 0, "4": 0, "5": 1}
    assert (r["valid_rollouts"], r["invalid_rollouts"]) == (11, 1)
    assert r["cost_usd"] == {"authoring": 0.5, "evaluation": 0.05, "total": 0.55, "per_accepted": 0.55}
    assert r["config_hash"] == "cfg1"
    assert r["wall_clock_s"] is not None
    # A subset, and everything created since a time, use the same report.
    assert client.get("/v1/report", params={"task_id": ["t-02"]}, headers=AUTH).json()["candidates"] == 1
    assert client.get("/v1/report", params={"since": "2000-01-01"}, headers=AUTH).json()["candidates"] == 3


def test_fresh_report_has_nulls_not_zeros(client):
    ids = [create(client)["task_id"] for _ in range(2)]
    r = client.get("/v1/report", params={"task_id": ids}, headers=AUTH).json()
    assert r["cost_usd"] == {"authoring": None, "evaluation": None, "total": None, "per_accepted": None}
    assert r["wall_clock_s"] is None and r["config_hash"] is None
