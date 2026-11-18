"""reuse_worlds (docs/HILLCLIMBING_ALGORITHM.md "reuse worlds"): new tasks start at create on a
checked world from the pool instead of building one."""
import json

import pytest

from api.tests.test_api import AUTH, add_job, client, backend, rollout  # noqa: F401 (fixtures)
from api import submit
from shared.taxonomy import world_selection
from shared.taxonomy.world_selection import Use


def source(backend, tid, family, sha, status="REJECTED", cost=0.5, checked=True, mechanism="m1"):
    """A finished task that built world `sha` (world job paid `cost`; world_check OK if `checked`)."""
    with backend.store.tx() as conn:
        conn.execute("INSERT INTO tasks (id, slot, status, current_version, stage, created_at, updated_at)"
                     " VALUES (?, ?, ?, 1, 'INVALID', '2026-01-01', '2026-01-01')",
                     (tid, json.dumps({"family": family, "mechanism": mechanism, "shape": "fix", "dial": 3,
                                       "seed": f"a {family} system"}), status))
    add_job(backend, tid, 1, "world", f"{tid}-v1-world", outcome="OK", cost_usd=cost,
            result={"world_sha256": sha})
    add_job(backend, tid, 1, "world_check", f"{tid}-v1-world-check-r0",
            outcome="OK" if checked else "FAILED", input={"world_sha256": sha})


def test_the_pool_is_checked_paid_worlds_only(backend):
    source(backend, "a", "compilers_parsers", "w-good")
    source(backend, "b", "compilers_parsers", "w-mock", cost=0.0)        # a mock run
    source(backend, "c", "compilers_parsers", "w-unchecked", checked=False)
    with backend.store.tx() as conn:
        pool = submit.world_pool(conn)
    assert [w["world_sha256"] for w in pool] == ["w-good"]
    assert (pool[0]["source_task"], pool[0]["family"], pool[0]["reuses"]) == ("a", "compilers_parsers", 0)


def test_pick_follows_the_selection_policy_then_falls_back():
    pool = [{"world_sha256": "acc", "family": "f1", "source_status": "ACCEPTED", "reuses": 0},
            {"world_sha256": "easy", "family": "f1", "source_status": "REJECTED", "reuses": 0},
            {"world_sha256": "near", "family": "f1", "source_status": "REJECTED", "reuses": 0},
            {"world_sha256": "g", "family": "f2", "source_status": "REJECTED", "reuses": 2}]
    uses = {"acc": [Use(pass_count=2, accepted=True)], "easy": [Use(pass_count=5)], "near": [Use(pass_count=4)]}
    picks = submit.pick_worlds(pool, 4, uses=uses)
    # The policy: f1's 4/5 world, then its 5/5 one (acc has an accepted task; g is at the reuse
    # cap). Then the fallback spreads the rest: f2's only world, twice.
    assert [(w["world_sha256"], w["tier"]) for w in picks] == [
        ("near", "near"), ("easy", "easy"), ("g", "fallback"), ("g", "fallback")]
    assert "4/5" in picks[0]["reason"]
    with pytest.raises(ValueError, match="no checked world"):
        submit.pick_worlds(pool, 1, area="f3")


def test_world_uses_reads_each_versions_world_and_score_from_the_jobs(backend):
    source(backend, "src", "compilers_parsers", "w1", status="CALIBRATING")
    add_job(backend, "src", 1, "create", "src-v1-create", outcome="OK", input={"world_sha256": "w1"})
    for slot, outcome in enumerate(["VALID_PASS"] * 4 + ["VALID_FAIL"]):
        rollout(backend, "src", 1, slot, outcome)
    with backend.store.tx() as conn:
        uses = world_selection.world_uses(conn)
    # Its task is still in flight, so the use is live (its domain + mechanism is being built).
    assert uses == {"w1": [Use(pass_count=4, accepted=False, leak_rejected=False, new_prompts=True, live=True)]}


def test_a_rejection_only_the_blind_predictor_caused_is_a_blind_leak(backend):
    source(backend, "blind", "algorithmic_constraints", "wb", status="REJECTED")
    source(backend, "semantic", "algorithmic_constraints", "ws", status="REJECTED")
    with backend.store.tx() as conn:
        conn.execute("UPDATE tasks SET reason = 'invalid'")
    for tid, sha, leak in (("blind", "wb", {"passed": False, "verdict": "clean", "blind": {"prediction": {}}}),
                           ("semantic", "ws", {"passed": False, "verdict": "leak", "blind": None})):
        add_job(backend, tid, 1, "create", f"{tid}-v1-create", outcome="FAILED", input={"world_sha256": sha},
                result={"leak": leak})
    with backend.store.tx() as conn:
        uses = world_selection.world_uses(conn)
    assert [(u.leak_rejected, u.blind_leak) for u in uses["wb"]] == [(True, True)]
    assert [(u.leak_rejected, u.blind_leak) for u in uses["ws"]] == [(True, False)]


def test_the_diversity_summary_counts_families_domains_and_reuses(backend):
    source(backend, "src-a", "database_consistency", "wa", status="CALIBRATING")
    source(backend, "src-b", "database_consistency", "wb", status="CALIBRATING")
    with backend.store.tx() as conn:
        conn.execute("UPDATE jobs SET result = ? WHERE id = 'src-a-v1-world'",
                     (json.dumps({"world_sha256": "wa", "name": "payment-settlement-recovery"}),))
        conn.execute("UPDATE jobs SET result = ? WHERE id = 'src-b-v1-world'",
                     (json.dumps({"world_sha256": "wb", "name": "charge-ledger-retry"}),))
        text = world_selection.diversity_summary(conn, since="2026-01-01")
    assert "tasks since 2026-01-01: 2 (0 on a reused world)" in text
    assert "domains (1): ledger/payments 2" in text
    assert "repeated domain + mechanism: ledger/payments + m1 2" in text


def test_a_reuse_batch_starts_at_create_on_pooled_worlds(client, backend):
    source(backend, "src-1", "compilers_parsers", "w1")
    source(backend, "src-2", "database_consistency", "w2", status="ACCEPTED")
    r = client.post("/v1/batches", json={"batch_id": "aws-w2-l5", "count": 2, "reuse_worlds": True,
                                         "area": ["compilers_parsers", "database_consistency"]}, headers=AUTH)
    assert r.status_code == 202, r.text
    worlds = set()
    for tid in r.json()["task_ids"]:
        task = backend.store.task(tid)
        (job,) = backend.store.task_jobs(tid)
        assert job["type"] == "create" and job["id"] == f"{tid}-v1-create"
        # Neither world has a scored task yet: both are "unscored", recorded on the slot and the job.
        assert task["slot"]["reuse_tier"] == "unscored"
        assert job["input"] == {"origin": "initial", "world_sha256": task["slot"]["reused_world"],
                                "shape": "fix", "dial": 3, "reused": True, "reuse_tier": "unscored",
                                "reuse_reason": task["slot"]["reuse_reason"]}
        assert task["slot"]["reused_from"] in ("src-1", "src-2") and task["slot"]["seed"]
        worlds.add(task["slot"]["reused_world"])
    assert worlds == {"w1", "w2"}  # one per family
    listing = {w["world_sha256"]: w["reuses"] for w in client.get("/v1/worlds", headers=AUTH).json()}
    assert listing == {"w1": 1, "w2": 1}
    # The same request again is a no-op; with other options it's a 4xx.
    again = client.post("/v1/batches", json={"batch_id": "aws-w2-l5", "count": 2, "reuse_worlds": True,
                                             "area": ["compilers_parsers", "database_consistency"]}, headers=AUTH)
    assert again.status_code == 202 and again.json()["created"] is False
    bad = client.post("/v1/batches", json={"count": 1, "reuse_worlds": True, "prompt": "x"}, headers=AUTH)
    assert bad.status_code in (400, 422)


def test_the_task_view_shows_the_reused_world_while_create_runs(client, backend):
    source(backend, "src-1", "compilers_parsers", "w1")
    tid = client.post("/v1/batches", json={"batch_id": "rv", "count": 1, "reuse_worlds": True},
                      headers=AUTH).json()["task_ids"][0]
    d = client.get(f"/v1/tasks/{tid}", headers=AUTH).json()
    # Nothing has a result yet: the world comes from the create job's input.
    assert d["versions"][0]["world"]["world_sha256"] == "w1" and d["versions"][0]["world"]["reused"] is True
    assert d["reuse"]["world_sha256"] == "w1" and d["reuse"]["from_task"] == "src-1"
    assert client.get("/v1/tasks/src-1", headers=AUTH).json()["reuse"] is None
