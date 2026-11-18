"""API1 (docs/AGENTIC_AUTHORING_PLAN.md §3.1-3.2): taxonomy v2, spec v2, areas and
filters on tasks and batches, the first job by author mode, reports by family and
mechanism, and the task's world."""

import random
from collections import Counter

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from api.tests.test_api import AUTH, KEY, add_job, rollout, set_task
from shared.contracts import models
from shared.storage import from_env
from shared.storage.sql import job_id
from shared.taxonomy.taxonomy import (
    DEFAULT_DIAL,
    DIALS,
    ENVIRONMENT_IMAGES,
    FAMILIES,
    FAMILY_WEIGHTS,
    legacy_view,
    resolve_area,
    sample_spec_v2,
    shape_for,
)

V2_KEYS = {"family", "mechanism", "shape", "environment", "verification", "dial"}


@pytest.fixture
def backend(tmp_path):
    return from_env({"SDE_WORK_DIR": str(tmp_path / "work")})


@pytest.fixture
def client(backend):
    return TestClient(create_app(backend, api_key=KEY))


@pytest.fixture
def agent_mode(monkeypatch):
    monkeypatch.setattr(models, "AUTHOR_MODE", "agent")


def batch(client, **body):
    r = client.post("/v1/batches", json=body, headers=AUTH)
    assert r.status_code == 202, r.text
    return r.json()


def slots(backend, task_ids):
    return [backend.store.task(t)["slot"] for t in task_ids]


def first_job(backend, task_id):
    return next(j for j in backend.store.task_jobs(task_id) if j["version"] == 1)


def assert_valid_v2(spec):
    fam = FAMILIES[spec["family"]]
    assert spec["mechanism"] in fam.mechanisms and spec["environment"] in fam.environments
    assert spec["verification"] in fam.verification and spec["dial"] in DIALS
    assert spec["shape"] == shape_for(spec["mechanism"])
    assert {k: spec[k] for k in ("taxonomy", "difficulty")} == legacy_view(spec)  # one-shot view


# --- the sampler -------------------------------------------------------------------


def test_v2_specs_are_valid_and_carry_the_one_shot_view():
    rng = random.Random(1)
    existing = []
    for _ in range(40):
        spec = sample_spec_v2(existing, rng=rng)
        assert V2_KEYS <= spec.keys()
        assert_valid_v2(spec)
        existing.append(spec)


def test_v2_sampler_balances_families_and_covers_each_familys_combinations():
    existing = []
    for i in range(72):
        existing.append(sample_spec_v2(existing, rng=random.Random(i)))
        counts = Counter(s["family"] for s in existing)
        assert max(counts.values()) - min(counts.get(f, 0) for f in FAMILIES) <= 1  # balanced at every step
    for f, fam in FAMILIES.items():  # 12 picks each: every combination before any repeats
        picks = [(s["mechanism"], s["environment"]) for s in existing if s["family"] == f]
        n_combos = len(fam.mechanisms) * len(fam.environments)
        assert len(set(picks[:n_combos])) == n_combos


def test_every_environment_maps_to_a_pinned_base_image():
    import json
    from pathlib import Path

    pinned = json.loads((Path(__file__).resolve().parents[2] / "config" / "base_images.json").read_text())
    envs = {e for fam in FAMILIES.values() for e in fam.environments}
    assert envs <= ENVIRONMENT_IMAGES.keys()
    assert {ENVIRONMENT_IMAGES[e] for e in envs} <= pinned.keys()


def test_family_weights_steer_the_spread(monkeypatch):
    assert set(FAMILY_WEIGHTS) == set(FAMILIES) and set(FAMILY_WEIGHTS.values()) == {1.0}
    monkeypatch.setitem(FAMILY_WEIGHTS, "compilers_parsers", 3.0)
    existing = []
    for i in range(80):
        existing.append(sample_spec_v2(existing, rng=random.Random(i)))
    counts = Counter(s["family"] for s in existing)
    assert counts["compilers_parsers"] == 30 and all(n == 10 for f, n in counts.items() if f != "compilers_parsers")


def test_dial_defaults_to_two_and_can_be_set():
    assert sample_spec_v2([])["dial"] == DEFAULT_DIAL == 2
    assert sample_spec_v2([], dial=3)["dial"] == 3


def test_shape_follows_the_mechanism():
    assert shape_for("optimality_required") == "implement"
    assert shape_for("scalability_wall") == shape_for("memory_bound") == "optimize"
    assert shape_for("isolation_anomaly") == shape_for("greedy_counterexample") == "fix"
    assert shape_for("add_schema_version") == "migrate"


def test_area_and_filters_limit_the_sample():
    existing = []
    for _ in range(12):
        spec = sample_spec_v2(existing, area=["compilers_parsers", "algorithmic_constraints"])
        assert spec["family"] in ("compilers_parsers", "algorithmic_constraints")
        existing.append(spec)
    assert {s["family"] for s in existing} == {"compilers_parsers", "algorithmic_constraints"}
    s = sample_spec_v2([], area="database_consistency", mechanism="write_skew",
                       environment="PostgreSQL database", dial=3)
    assert (s["family"], s["mechanism"], s["environment"], s["dial"]) == (
        "database_consistency", "write_skew", "PostgreSQL database", 3)
    # A mechanism alone picks its family.
    assert sample_spec_v2([], mechanism="fragmented_frames")["family"] == "compilers_parsers"


@pytest.mark.parametrize("kwargs, message", [
    ({"area": "networking"}, "valid areas"),
    ({"area": []}, "valid areas"),
    ({"area": "database_consistency", "mechanism": "fragmented_frames"}, "allowed"),
    ({"area": "compilers_parsers", "environment": "Java application"}, "allowed"),
    ({"dial": 4}, "dial"),
])
def test_impossible_requests_say_what_is_allowed(kwargs, message):
    with pytest.raises(ValueError, match=message):
        sample_spec_v2([], **kwargs)


def test_resolve_area():
    assert resolve_area(None) == list(FAMILIES)
    assert resolve_area("compilers_parsers") == ["compilers_parsers"]
    assert resolve_area(["compilers_parsers", "compilers_parsers"]) == ["compilers_parsers"]


# --- GET /v1/families -------------------------------------------------------------------


def test_families_lists_every_area_and_its_mechanisms(client):
    body = client.get("/v1/families", headers=AUTH).json()
    assert set(body) == set(FAMILIES)
    db = body["database_consistency"]
    assert db["mechanisms"] == list(FAMILIES["database_consistency"].mechanisms)
    assert db["environments"] == ["Python service (SQLite)", "PostgreSQL database"]
    assert body["algorithmic_constraints"]["shapes"] == ["fix", "implement"]
    assert client.get("/v1/families").status_code == 401


# --- batches by area ---------------------------------------------------------------------


def test_batch_within_one_area_spreads_its_combinations(client, backend):
    body = batch(client, count=10, area="database_consistency", batch_id="b-db")
    specs = slots(backend, body["task_ids"])
    assert {s["family"] for s in specs} == {"database_consistency"}
    for s in specs:
        assert_valid_v2(s)
    # 4 mechanisms x 2 environments = 8 combinations: all used before any repeats.
    assert len({(s["mechanism"], s["environment"]) for s in specs}) == 8
    assert {s["dial"] for s in specs} == {DEFAULT_DIAL}  # the dial stays at its default unless asked


def test_batch_within_a_list_of_areas(client, backend):
    body = batch(client, count=10, area=["compilers_parsers", "algorithmic_constraints"])
    specs = slots(backend, body["task_ids"])
    assert {s["family"] for s in specs} == {"compilers_parsers", "algorithmic_constraints"}


def test_batch_without_area_in_one_shot_mode_is_unchanged(client, backend):
    specs = slots(backend, batch(client, count=4)["task_ids"])
    assert all("family" not in s and s["taxonomy"]["domain"] for s in specs)  # v1, as before
    assert all(first_job(backend, t)["type"] == "create"
               for t in batch(client, count=1)["task_ids"])


def test_batch_without_area_in_agent_mode_samples_the_whole_taxonomy(client, backend, agent_mode):
    specs = slots(backend, batch(client, count=12)["task_ids"])
    assert {s["family"] for s in specs} == set(FAMILIES)  # 12 tasks, least-used: all six families


def test_batch_finer_filters(client, backend):
    specs = slots(backend, batch(client, count=3, area="database_consistency", mechanism="idempotent_retry",
                                 dial=1)["task_ids"])
    assert {(s["mechanism"], s["dial"]) for s in specs} == {("idempotent_retry", 1)}
    assert {s["environment"] for s in specs} == set(FAMILIES["database_consistency"].environments)


@pytest.mark.parametrize("body, needle", [
    ({"count": 2, "area": "networking"}, "valid areas"),
    ({"count": 2, "area": ["compilers_parsers", "nope"]}, "valid areas"),
    ({"count": 2, "area": "compilers_parsers", "mechanism": "write_skew"}, "allowed"),
    ({"count": 2, "dial": 0}, ""),
    ({"count": 2, "area": "compilers_parsers", "spec": {"family": "compilers_parsers"}}, "not both"),
])
def test_bad_batch_areas_and_filters_are_422(client, body, needle):
    r = client.post("/v1/batches", json=body, headers=AUTH)
    assert r.status_code == 422, r.text
    assert needle in r.text


def test_batch_with_an_area_is_idempotent_and_conflicts_on_a_different_area(client):
    first = batch(client, count=3, area="compilers_parsers", batch_id="b-x")
    again = batch(client, count=3, area="compilers_parsers", batch_id="b-x")
    assert again == {**first, "created": False}
    r = client.post("/v1/batches", json={"count": 3, "area": "database_consistency", "batch_id": "b-x"},
                    headers=AUTH)
    assert r.status_code == 409


def test_a_prompt_seeds_every_task_of_an_area_batch(client, backend):
    specs = slots(backend, batch(client, count=3, area="cross_component_stateful",
                                 prompt="a hotel booking platform")["task_ids"])
    assert {s["seed"] for s in specs} == {"a hotel booking platform"}
    assert {s["family"] for s in specs} == {"cross_component_stateful"}


# --- single tasks --------------------------------------------------------------------


def post_task(client, **body):
    r = client.post("/v1/tasks", json=body, headers=AUTH)
    assert r.status_code == 202, r.text
    return r.json()["task_id"]


def test_task_with_area_and_prompt(client, backend):
    t = post_task(client, area="performance_resources", prompt="a log compactor", dial=2)
    s = backend.store.task(t)["slot"]
    assert (s["family"], s["seed"], s["dial"]) == ("performance_resources", "a log compactor", 2)
    assert_valid_v2(s)


def test_explicit_v2_spec_is_validated_and_filled(client, backend):
    t = post_task(client, spec={"family": "compilers_parsers", "mechanism": "fragmented_frames",
                                "design": ["the parser is incremental"]})
    s = backend.store.task(t)["slot"]
    assert (s["family"], s["mechanism"], s["shape"]) == ("compilers_parsers", "fragmented_frames", "fix")
    assert s["design"] == ["the parser is incremental"] and s["environment"] in ("Python service", "Go service")
    t2 = post_task(client, spec={"family": "algorithmic_constraints", "mechanism": "greedy_counterexample",
                                 "shape": "implement"})
    s2 = backend.store.task(t2)["slot"]
    assert s2["shape"] == "implement" and s2["taxonomy"]["task_type"] == "Implementation"  # view follows


@pytest.mark.parametrize("spec", [
    {"family": "networking"},
    {"family": "compilers_parsers", "mechanism": "write_skew"},
    {"family": "compilers_parsers", "environment": "C program"},
    {"family": "compilers_parsers", "shape": "rewrite"},
])
def test_bad_explicit_v2_specs_are_422(client, spec):
    assert client.post("/v1/tasks", json={"spec": spec}, headers=AUTH).status_code == 422


def test_raw_prompt_cannot_be_combined_with_filters(client):
    r = client.post("/v1/tasks", json={"raw_prompt": "x", "area": "compilers_parsers"}, headers=AUTH)
    assert r.status_code == 422


def test_task_id_retry_with_a_different_area_conflicts(client):
    post_task(client, area="compilers_parsers", task_id="t-x")
    assert post_task(client, area="compilers_parsers", task_id="t-x") == "t-x"
    r = client.post("/v1/tasks", json={"area": "database_consistency", "task_id": "t-x"}, headers=AUTH)
    assert r.status_code == 409


# --- the first job ------------------------------------------------------------------------


def test_one_shot_mode_starts_with_a_create_job(client, backend):
    t = post_task(client, area="compilers_parsers")
    job = first_job(backend, t)
    assert (job["id"], job["type"]) == (job_id(t, 1, "create"), "create")
    assert job["input"] == {"origin": "initial", "spec": backend.store.task(t)["slot"]}


def test_agent_mode_starts_with_a_world_job(client, backend, agent_mode):
    t = post_task(client, prompt="an inventory reservation service")
    job, task = first_job(backend, t), backend.store.task(t)
    assert (job["id"], job["type"], job["status"]) == (f"{t}-v1-world", "world", "READY")
    assert job["input"] == {"origin": "initial", "spec": task["slot"]}
    assert {"shape", "dial"} <= job["input"]["spec"].keys()  # world_check → create copies them
    assert (task["status"], task["stage"]) == ("CREATED", "QUEUED")
    for tid in batch(client, count=3, area="database_consistency")["task_ids"]:
        assert first_job(backend, tid)["type"] == "world"


def test_agent_mode_keeps_raw_prompts_and_v1_specs_on_create(client, backend, agent_mode):
    raw = post_task(client, raw_prompt="write a task about X")
    v1 = post_task(client, spec={"taxonomy": {"domain": "databases"}})
    assert first_job(backend, raw)["type"] == first_job(backend, v1)["type"] == "create"


# --- reports and the task view ---------------------------------------------------------------


def test_reports_break_down_by_family_and_mechanism(client, backend):
    ids = batch(client, count=4, area="database_consistency", mechanism="write_skew", batch_id="b-r")["task_ids"]
    legacy = post_task(client)  # a v1 task: left out of the breakdowns
    for slot, outcome in enumerate(["VALID_PASS", "VALID_FAIL", "VALID_FAIL", "VALID_PASS", "VALID_FAIL"], 1):
        rollout(backend, ids[0], 1, slot, outcome, cost_usd=0.01)
    rollout(backend, ids[1], 1, 1, "INVALID", error="AgentTimeoutError")  # not scored
    # Agent-mode authoring spend: the world and its check count as authoring.
    with backend.store.tx() as conn:
        conn.execute("UPDATE jobs SET cost_usd = 2.0 WHERE id = ?", (job_id(ids[0], 1, "create"),))
    add_job(backend, ids[0], 1, "world_check", job_id(ids[0], 1, "world-check"), outcome="OK", cost_usd=0.0)
    set_task(backend, ids[0], status="ACCEPTED", accepted_version=1, stage="BAND_OK")
    set_task(backend, ids[1], status="REJECTED", reason="exhausted", stage="OUT_OF_BAND")

    report = client.get("/v1/batches/b-r/report", headers=AUTH).json()
    assert report["by_family"] == {"database_consistency": {
        "candidates": 4, "finished": 2, "accepted": 1, "rejected": 1, "learnable_rate": 0.5,
        "valid_rollouts": 5, "pass_rate": 0.4,
        "cost_usd": {"authoring": 2.0, "evaluation": 0.05, "authoring_per_task": 0.5}}}
    assert report["by_mechanism"]["write_skew"]["pass_rate"] == 0.4
    assert report["cost_usd"]["authoring_per_task"] == 0.5 and report["cost_usd"]["evaluation_per_task"] == 0.0125
    whole = client.get("/v1/report", params={"task_id": [*ids, legacy]}, headers=AUTH).json()
    assert set(whole["by_family"]) == {"database_consistency"} and whole["candidates"] == 5


def test_task_view_shows_the_world_and_its_lineage(client, backend, agent_mode):
    t = post_task(client, area="database_consistency")
    assert client.get(f"/v1/tasks/{t}", headers=AUTH).json()["versions"][0]["world"] == {
        "world_sha256": None, "lineage": None, "status": "READY", "outcome": None}
    with backend.store.tx() as conn:
        conn.execute("UPDATE jobs SET status = 'DONE', outcome = 'OK', result = ? WHERE id = ?",
                     ('{"world_sha256": "w-abc", "lineage": {"parent_world_sha256": null, "mutations": []}}',
                      f"{t}-v1-world"))
    world = client.get(f"/v1/tasks/{t}", headers=AUTH).json()["versions"][0]["world"]
    assert world == {"world_sha256": "w-abc", "lineage": {"parent_world_sha256": None, "mutations": []},
                     "status": "DONE", "outcome": "OK"}


def test_one_shot_task_view_has_no_world(client, backend):
    t = post_task(client)
    add_job(backend, t, 1, "validate", job_id(t, 1, "validate"), outcome="OK", result={"gates": {}})
    assert client.get(f"/v1/tasks/{t}", headers=AUTH).json()["versions"][0]["world"] is None
