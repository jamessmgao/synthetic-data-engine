import json
import threading

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from api.config import Settings
from api.db import connect, utcnow

KEY = "test-key"
AUTH = {"X-Api-Key": KEY}


@pytest.fixture
def settings(tmp_path):
    return Settings(db_path=tmp_path / "sde.db", api_key=KEY)


@pytest.fixture
def client(settings):
    return TestClient(create_app(settings))


@pytest.fixture
def conn(settings, client):  # client first, so the schema exists
    c = connect(settings.db_path)
    yield c
    c.close()


def add_job(conn, task_id, version, type_, **cols):
    """Write a settled job row the way a worker would."""
    slot = cols.pop("slot", None)
    retry = cols.pop("retry", 0)
    job_id = f"{task_id}-v{version}-{type_}{slot or ''}-r{retry}"
    for key in ("input", "result", "failure", "trace_metrics"):
        if key in cols:
            cols[key] = json.dumps(cols[key])
    now = utcnow()
    base = dict(id=job_id, task_id=task_id, version=version, type=type_, slot=slot, retry=retry,
                status="DONE", due_at=now, priority=0, created_at=now, finished_at=now)
    base.update(cols)
    names = ", ".join(base)
    conn.execute(f"INSERT INTO jobs ({names}) VALUES ({', '.join('?' * len(base))})", tuple(base.values()))


def add_rollouts(conn, task_id, version, pattern, cost=0.01):
    for slot, mark in enumerate(pattern, start=1):
        add_job(conn, task_id, version, "rollout", slot=slot, config_hash="cfg1", cost_usd=cost,
                outcome="VALID_PASS" if mark == "1" else "VALID_FAIL", reward=float(mark))


GOOD_GATES = {"schema": True, "structural": True, "lint": True, "oracle": [1, 1], "nop": 0}


# --- auth and health --------------------------------------------------------


def test_requires_api_key(client):
    assert client.post("/v1/batches", json={"count": 1}).status_code == 401
    assert client.post("/v1/batches", json={"count": 1}, headers={"X-Api-Key": "wrong"}).status_code == 401


def test_closed_without_configured_key(tmp_path):
    client = TestClient(create_app(Settings(db_path=tmp_path / "sde.db", api_key="")))
    assert client.get("/v1/batches/x", headers={"X-Api-Key": ""}).status_code == 503


def test_healthz_needs_no_key(client):
    r = client.get("/healthz")
    assert r.status_code == 200 and r.json() == {"status": "ok", "db": "ok"}


# --- submission -------------------------------------------------------------


def test_submit_creates_tasks_and_generate_jobs(client, conn):
    r = client.post("/v1/batches", json={"count": 3, "batch_id": "b-1"}, headers=AUTH)
    assert r.status_code == 202
    assert r.json() == {"batch_id": "b-1", "task_ids": ["b-1-t01", "b-1-t02", "b-1-t03"], "created": True}

    tasks = conn.execute("SELECT id, status, stage, current_version FROM tasks ORDER BY id").fetchall()
    assert [tuple(t) for t in tasks] == [(f"b-1-t0{i}", "CREATED", "QUEUED", 1) for i in (1, 2, 3)]
    jobs = conn.execute("SELECT id, type, status, input FROM jobs ORDER BY id").fetchall()
    assert [j["id"] for j in jobs] == [f"b-1-t0{i}-v1-generate-r0" for i in (1, 2, 3)]
    assert all(j["type"] == "generate" and j["status"] == "READY" for j in jobs)
    assert json.loads(jobs[0]["input"]) == {"origin": "initial"}


def test_submit_is_idempotent_on_batch_id(client, conn):
    first = client.post("/v1/batches", json={"count": 2, "batch_id": "b-1"}, headers=AUTH).json()
    again = client.post("/v1/batches", json={"count": 2, "batch_id": "b-1"}, headers=AUTH)
    assert again.status_code == 202
    assert again.json() == {**first, "created": False}
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 2


def test_submit_conflicting_count_is_409(client, conn):
    client.post("/v1/batches", json={"count": 2, "batch_id": "b-1"}, headers=AUTH)
    r = client.post("/v1/batches", json={"count": 3, "batch_id": "b-1"}, headers=AUTH)
    assert r.status_code == 409
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 2


def test_submit_generates_batch_id(client):
    body = client.post("/v1/batches", json={}, headers=AUTH).json()
    assert body["batch_id"].startswith("b-") and len(body["task_ids"]) == 10


@pytest.mark.parametrize("body", [{"count": 0}, {"count": 51}, {"batch_id": "Bad/Id"}, {"count": "x"}])
def test_submit_validation_is_422(client, body):
    assert client.post("/v1/batches", json=body, headers=AUTH).status_code == 422


def test_concurrent_submissions_create_one_batch(settings, client, conn):
    from api import service

    errors = []

    def submit():
        c = connect(settings.db_path)
        try:
            service.submit_batch(c, 5, "b-race")
        except Exception as e:  # pragma: no cover - reported below
            errors.append(e)
        finally:
            c.close()

    threads = [threading.Thread(target=submit) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 5
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 5


# --- status, verdict, report ------------------------------------------------


def test_unknown_ids_are_404(client):
    assert client.get("/v1/batches/nope", headers=AUTH).status_code == 404
    assert client.get("/v1/batches/nope/report", headers=AUTH).status_code == 404
    assert client.get("/v1/tasks/nope", headers=AUTH).status_code == 404


@pytest.fixture
def worked_batch(client, conn):
    """t01: v1 too easy (5/5), v2 accepted at 2/5. t02: rejected invalid. t03: mid-evaluation."""
    client.post("/v1/batches", json={"count": 3, "batch_id": "b-1"}, headers=AUTH)

    add_job(conn, "b-1-t01", 1, "validate", outcome="OK", result={"gates": GOOD_GATES})
    add_rollouts(conn, "b-1-t01", 1, "11111")
    add_job(conn, "b-1-t01", 2, "generate", outcome="OK", bundle_sha256="sha-v2", cost_usd=0.5,
            input={"origin": "revision", "operator": "increase_diagnosis_depth"})
    add_job(conn, "b-1-t01", 2, "validate", outcome="OK", result={"gates": GOOD_GATES})
    # An invalid (timed-out) rollout retried in the same slot.
    add_job(conn, "b-1-t01", 2, "rollout", slot=1, retry=0, outcome="INVALID", error="AgentTimeoutError")
    add_job(conn, "b-1-t01", 2, "rollout", slot=1, retry=1, outcome="VALID_FAIL", reward=0.0,
            config_hash="cfg1", failure={"explanation": "missed the retry path", "attribution": "model"})
    for slot, mark in zip(range(2, 6), "1010"):
        add_job(conn, "b-1-t01", 2, "rollout", slot=slot, config_hash="cfg1",
                outcome="VALID_PASS" if mark == "1" else "VALID_FAIL", reward=float(mark))
    conn.execute("UPDATE tasks SET status='ACCEPTED', current_version=2, stage='BAND_OK',"
                 " accepted_version=2 WHERE id='b-1-t01'")

    add_job(conn, "b-1-t02", 1, "validate", outcome="FAILED",
            result={"gates": {**GOOD_GATES, "oracle": [0, 0]}})
    conn.execute("UPDATE tasks SET status='REJECTED', reason='invalid', stage='INVALID' WHERE id='b-1-t02'")

    add_job(conn, "b-1-t03", 1, "validate", outcome="OK", result={"gates": GOOD_GATES})
    add_job(conn, "b-1-t03", 1, "rollout", slot=2, outcome="VALID_PASS", reward=1.0, config_hash="cfg1")
    conn.execute("UPDATE tasks SET status='CALIBRATING', stage='EVALUATING' WHERE id='b-1-t03'")


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


def test_scored_slot_is_unique(client, conn):
    import sqlite3

    client.post("/v1/batches", json={"count": 1, "batch_id": "b-1"}, headers=AUTH)
    add_job(conn, "b-1-t01", 1, "rollout", slot=1, retry=0, outcome="VALID_PASS", reward=1.0)
    with pytest.raises(sqlite3.IntegrityError):
        add_job(conn, "b-1-t01", 1, "rollout", slot=1, retry=1, outcome="VALID_FAIL", reward=0.0)
