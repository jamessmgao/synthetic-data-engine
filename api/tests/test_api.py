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
    assert client.post("/v1/batches", json={"count": 1}).status_code == 401
    assert client.post("/v1/batches", json={"count": 1}, headers={"X-Api-Key": "wrong"}).status_code == 401


def test_closed_without_configured_key(backend):
    client = TestClient(create_app(backend, api_key=""))
    assert client.get("/v1/batches/x", headers={"X-Api-Key": ""}).status_code == 503


def test_healthz_needs_no_key(client):
    body = client.get("/healthz").json()
    assert body["status"] == "ok" and body["backends"]["store"] == "sqlite"


# --- submission -------------------------------------------------------------


def test_submit_goes_through_the_job_service(client, backend):
    r = client.post("/v1/batches", json={"count": 3, "batch_id": "b-1"}, headers=AUTH)
    assert r.status_code == 202
    assert r.json() == {"batch_id": "b-1", "task_ids": ["b-1-t01", "b-1-t02", "b-1-t03"], "created": True}
    gen = backend.store.job(job_id("b-1-t01", 1, "generate"))
    assert gen["status"] == "READY" and gen["input"]["origin"] == "initial" and gen["input"]["spec"]


def test_submit_is_idempotent_and_409_on_conflict(client, backend):
    first = client.post("/v1/batches", json={"count": 2, "batch_id": "b-1"}, headers=AUTH).json()
    again = client.post("/v1/batches", json={"count": 2, "batch_id": "b-1"}, headers=AUTH)
    assert again.status_code == 202 and again.json() == {**first, "created": False}
    assert client.post("/v1/batches", json={"count": 3, "batch_id": "b-1"}, headers=AUTH).status_code == 409
    assert len(backend.store.batch_tasks("b-1")) == 2


def test_submit_generates_batch_id(client):
    body = client.post("/v1/batches", json={}, headers=AUTH).json()
    assert body["batch_id"].startswith("b-") and len(body["task_ids"]) == 10


@pytest.mark.parametrize("body", [{"count": 0}, {"count": 51}, {"batch_id": "Bad/Id"}, {"count": "x"}])
def test_submit_validation_is_422(client, body):
    assert client.post("/v1/batches", json=body, headers=AUTH).status_code == 422


# --- status, verdict, report ------------------------------------------------


def test_unknown_ids_are_404(client):
    assert client.get("/v1/batches/nope", headers=AUTH).status_code == 404
    assert client.get("/v1/batches/nope/report", headers=AUTH).status_code == 404
    assert client.get("/v1/tasks/nope", headers=AUTH).status_code == 404


@pytest.fixture
def worked_batch(client, backend):
    """t01: v1 too easy (5/5), v2 accepted at 2/5. t02: rejected invalid. t03: mid-evaluation."""
    client.post("/v1/batches", json={"count": 3, "batch_id": "b-1"}, headers=AUTH)
    t1, t2, t3 = "b-1-t01", "b-1-t02", "b-1-t03"

    add_job(backend, t1, 1, "validate", job_id(t1, 1, "validate", 0), outcome="OK", result={"gates": GOOD_GATES})
    for slot in range(1, 6):
        rollout(backend, t1, 1, slot, "VALID_PASS", cost_usd=0.01)
    add_job(backend, t1, 2, "generate", job_id(t1, 2, "generate"), outcome="OK", bundle_sha256="sha-v2",
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


def test_batch_list(client, worked_batch):
    (item,) = client.get("/v1/batches", headers=AUTH).json()
    assert (item["batch_id"], item["tasks"], item["accepted"], item["rejected"]) == ("b-1", 3, 1, 1)


def test_batch_status(client, worked_batch):
    body = client.get("/v1/batches/b-1", headers=AUTH).json()
    assert body["counts_by_status"] == {"ACCEPTED": 1, "REJECTED": 1, "CALIBRATING": 1}
    by_id = {t["id"]: t for t in body["tasks"]}
    assert by_id["b-1-t01"]["pass_pattern"] == "✗✓✗✓✗"
    assert by_id["b-1-t03"]["pass_pattern"] == "·✓···"
    assert by_id["b-1-t02"]["reason"] == "invalid"


def test_task_verdict(client, worked_batch):
    body = client.get("/v1/tasks/b-1-t01", headers=AUTH).json()
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

    pending = client.get("/v1/tasks/b-1-t03", headers=AUTH).json()
    assert pending["decision"] == "PENDING: v1 is EVALUATING"
    assert pending["versions"][0]["pass_count"] is None


def test_batch_report(client, worked_batch):
    r = client.get("/v1/batches/b-1/report", headers=AUTH).json()
    assert r["candidates"] == 3
    assert r["structural_pass"] == 3 and r["functional_pass"] == 2
    assert (r["accepted_v1"], r["accepted_total"]) == (0, 1)
    assert r["rejected"] == {"invalid": 1} and r["in_progress"] == 1
    assert r["pass_count_histogram"] == {"0": 0, "1": 0, "2": 1, "3": 0, "4": 0, "5": 1}
    assert (r["valid_rollouts"], r["invalid_rollouts"]) == (11, 1)
    assert r["cost_usd"] == {"authoring": 0.5, "evaluation": 0.05, "total": 0.55, "per_accepted": 0.55}
    assert r["config_hash"] == "cfg1"
    assert r["wall_clock_s"] is not None


def test_fresh_report_has_nulls_not_zeros(client):
    client.post("/v1/batches", json={"count": 2, "batch_id": "b-new"}, headers=AUTH)
    r = client.get("/v1/batches/b-new/report", headers=AUTH).json()
    assert r["cost_usd"] == {"authoring": None, "evaluation": None, "total": None, "per_accepted": None}
    assert r["wall_clock_s"] is None and r["config_hash"] is None
