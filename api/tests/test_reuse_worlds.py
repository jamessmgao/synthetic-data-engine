"""reuse_worlds (docs/HILLCLIMBING_ALGORITHM.md "reuse worlds"): new tasks start at create on a
checked world from the pool instead of building one."""
import json

import pytest

from api.tests.test_api import AUTH, add_job, client, backend, rollout  # noqa: F401 (fixtures)
from api import submit
from shared.taxonomy import world_selection
from shared.storage.sql import insert_job
from shared.contracts.models import JobType
from shared.taxonomy.taxonomy import FAMILIES
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


def test_pick_follows_the_selection_policy_and_refuses_what_it_cannot_fill():
    pool = [{"world_sha256": "acc", "family": "f1", "source_status": "ACCEPTED", "reuses": 0},
            {"world_sha256": "easy", "family": "f1", "source_status": "REJECTED", "reuses": 0},
            {"world_sha256": "near", "family": "f1", "source_status": "REJECTED", "reuses": 0},
            {"world_sha256": "leak", "family": "f2", "source_status": "REJECTED", "reuses": 0}]
    uses = {"acc": [Use(pass_count=2, accepted=True)], "easy": [Use(pass_count=5)], "near": [Use(pass_count=4)],
            "leak": [Use(leak_rejected=True, new_prompts=True)]}
    picks = submit.pick_worlds(pool, 3, uses=uses)
    # Families round-robin: f1's 4/5 world (f2's only world leaked under the current prompts),
    # then f1's others: an accepted world serves too, since a variant reseeds a new mechanism.
    assert [(w["world_sha256"], w["tier"]) for w in picks][0] == ("near", "near")
    assert {w["world_sha256"] for w in picks} == {"near", "easy", "acc"}
    assert "4/5" in picks[0]["reason"]
    # Never a leaked world to fill the count (unknown families serve once per call).
    with pytest.raises(ValueError, match="only 3 of 4 worlds"):
        submit.pick_worlds(pool, 4, uses=uses)
    with pytest.raises(ValueError, match="no checked world"):
        submit.pick_worlds(pool, 1, area="f3")


def test_novel_worlds_first_then_repeats_then_a_world_shared_with_its_live_task():
    pool = [{"world_sha256": s, "family": "f1", "mechanism": "m", "name": n, "source_status": "REJECTED",
             "reuses": 0} for s, n in (("live", "durable-order-ledger"), ("same", "retry-ledger-commit"),
                                       ("new", "booking-isolation"))]
    uses = {"live": [Use(live=True, task="src")], "same": [Use(pass_count=4, task="a")],
            "new": [Use(pass_count=5, task="b")]}
    picks = submit.pick_worlds(pool, 3, uses=uses)
    # The booking world is novel; the other ledger world repeats live ledger + m; the live world
    # itself serves last. Each pick's reason says so.
    assert [w["world_sha256"] for w in picks] == ["new", "same", "live"]
    assert "repeats a live" in picks[1]["reason"] and "its own task is live" in picks[2]["reason"]


def test_the_domain_cap_counts_every_familys_live_tasks():
    ledger = [{"world_sha256": f"l{i}", "family": "other_family", "mechanism": f"m{i}", "name": "x-ledger",
               "source_status": "REJECTED", "reuses": 0} for i in range(10)]
    mine = [{"world_sha256": s, "family": "f1", "mechanism": "k", "name": n, "source_status": "REJECTED",
             "reuses": 0} for s, n in (("ledger", "charge-ledger"), ("plan", "slot-scheduler"))]
    uses = {f"l{i}": [Use(live=True)] for i in range(10)}
    uses["ledger"], uses["plan"] = [Use(pass_count=4)], [Use(pass_count=5)]
    # 10 of 10 live tasks are ledgers: asked across two families, a ledger world is over the cap.
    with pytest.raises(ValueError, match="only 1 of 2"):
        submit.pick_worlds(ledger + mine, 2, area=["f1", "f9"], uses=uses)
    # Asked for one family (the user's exception), the cap is off; the rarer domain still goes first.
    picks = submit.pick_worlds(ledger + mine, 2, area="f1", uses=uses)
    assert [w["world_sha256"] for w in picks] == ["plan", "ledger"]


def test_world_uses_reads_each_versions_world_and_score_from_the_jobs(backend):
    source(backend, "src", "compilers_parsers", "w1", status="CALIBRATING")
    add_job(backend, "src", 1, "create", "src-v1-create", outcome="OK", input={"world_sha256": "w1"})
    for slot, outcome in enumerate(["VALID_PASS"] * 4 + ["VALID_FAIL"]):
        rollout(backend, "src", 1, slot, outcome)
    with backend.store.tx() as conn:
        uses = world_selection.world_uses(conn)
    # Its task is still in flight, so the use is live (its domain + mechanism is being built).
    assert uses == {"w1": [Use(pass_count=4, accepted=False, leak_rejected=False, new_prompts=True, live=True,
                               task="src")]}


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


def test_a_reuse_batch_starts_with_a_reseeded_variant_of_each_pooled_world(client, backend):
    source(backend, "src-1", "compilers_parsers", "w1", mechanism="aliasing_side_effects")
    source(backend, "src-2", "database_consistency", "w2", status="ACCEPTED", mechanism="write_skew")
    r = client.post("/v1/batches", json={"batch_id": "aws-w2-l5", "count": 2, "reuse_worlds": True,
                                         "area": ["compilers_parsers", "database_consistency"]}, headers=AUTH)
    assert r.status_code == 202, r.text
    worlds = set()
    for tid in r.json()["task_ids"]:
        task = backend.store.task(tid)
        slot = task["slot"]
        (job,) = backend.store.task_jobs(tid)
        # A light mutation of the picked world, on a reseeded spec: same family, another mechanism.
        assert job["type"] == "world" and job["id"] == f"{tid}-v1-world"
        inp = job["input"]
        assert inp["origin"] == "variant" and inp["parent_world_sha256"] == inp["variant_of"] == slot["reused_world"]
        assert slot["mechanism"] != slot["reseeded_from"] and inp["spec"]["mechanism"] == slot["mechanism"]
        assert inp["spec"]["family"] == slot["family"] and "reused_world" not in inp["spec"]
        assert len(inp["mutation"]["operators"]) == 2
        assert set(inp["mutation"]["operators"]) <= set(submit.VARIANT_OPERATORS)
        # Neither world has a scored task yet: both are "unscored", recorded on the slot and the job.
        assert slot["reuse_tier"] == inp["reuse_tier"] == "unscored" and inp["reused"] is True
        assert slot["reused_from"] in ("src-1", "src-2")
        worlds.add(slot["reused_world"])
    assert worlds == {"w1", "w2"}  # one per family
    listing = {w["world_sha256"]: w["reuses"] for w in client.get("/v1/worlds", headers=AUTH).json()}
    assert listing == {"w1": 1, "w2": 1}
    # The same request again is a no-op; with other options it's a 4xx.
    again = client.post("/v1/batches", json={"batch_id": "aws-w2-l5", "count": 2, "reuse_worlds": True,
                                             "area": ["compilers_parsers", "database_consistency"]}, headers=AUTH)
    assert again.status_code == 202 and again.json()["created"] is False
    bad = client.post("/v1/batches", json={"count": 1, "reuse_worlds": True, "prompt": "x"}, headers=AUTH)
    assert bad.status_code in (400, 422)


def test_reseeding_picks_an_untried_mechanism_never_greedy_and_counts_variants_as_reuses(backend):
    tried = set(FAMILIES["algorithmic_constraints"].mechanisms) - {"wraparound_boundary_in_sweep"}
    spec = submit.reseed_spec({"family": "algorithmic_constraints", "mechanism": "global_constraint_interaction",
                               "environment": "Python service", "domain": "calendar", "_world": "w"}, [], tried)
    # Every other mechanism was tried on this lineage: the one left is drawn; the domain is kept.
    assert spec["mechanism"] == "wraparound_boundary_in_sweep" and spec["family"] == "algorithmic_constraints"
    assert spec["domain"] == "calendar" and spec["taxonomy"]["domain"] == "calendar"
    assert spec["mechanism_note"].startswith("an exact sweep")
    # A world from before domains gets no invented domain: its code fixes what the system is.
    old = submit.reseed_spec({"family": "algorithmic_constraints", "mechanism": "global_constraint_interaction",
                              "environment": "Python service", "_world": "w"}, [])
    assert "domain" not in old and "domain_label" not in old
    assert old["taxonomy"]["domain"] == FAMILIES["algorithmic_constraints"].label
    source(backend, "src", "database_consistency", "w1", mechanism="write_skew")
    with backend.store.tx() as conn:
        insert_job(conn, id="new-v1-world", task_id="src", version=1, type=JobType.WORLD,
                   input={"origin": "variant", "parent_world_sha256": "w1", "variant_of": "w1"})
        insert_job(conn, id="new-v1-world-retry1", task_id="src", version=1, type=JobType.WORLD,
                   input={"origin": "variant", "parent_world_sha256": "w1", "variant_of": "w1"})
        (w,) = submit.world_pool(conn)
    assert w["reuses"] == 1  # the retry is the same task's variant


def test_a_lineage_serves_once_per_mechanism_it_has_not_had(backend):
    # aws-w2-l3-t01 / t03: two mutations of one source world are one lineage. The lineage has had
    # lost_acknowledged_write, so every other distributed_concurrency mechanism is a variant left.
    dist = FAMILIES["distributed_concurrency"].mechanisms
    free = len(dist) - 1
    source(backend, "src", "distributed_concurrency", "root", mechanism="lost_acknowledged_write")
    source(backend, "m1", "distributed_concurrency", "kid1", mechanism="lost_acknowledged_write")
    source(backend, "m2", "distributed_concurrency", "kid2", mechanism="lost_acknowledged_write")
    with backend.store.tx() as conn:
        for tid, kid in (("m1", "kid1"), ("m2", "kid2")):
            conn.execute("UPDATE jobs SET input = ? WHERE id = ?",
                         (json.dumps({"origin": "mutation", "parent_world_sha256": "root"}), f"{tid}-v1-world"))
        pool = submit.world_pool(conn)
    assert {w["world_sha256"]: w["lineage_root"] for w in pool} == {"root": "root", "kid1": "root", "kid2": "root"}
    assert pool[0]["lineage_mechanisms"] == ["lost_acknowledged_write"]
    picks = submit.pick_worlds(pool, 3)
    existing: list[dict] = []
    slots = []
    for w in picks:
        slots.append(submit._reused_slot(w, existing))
        existing.append(slots[-1])
    # Three variants, each of a different mechanism the lineage hadn't had.
    mechs = [s["mechanism"] for s in slots]
    assert len(set(mechs)) == 3 and "lost_acknowledged_write" not in mechs and set(mechs) <= set(dist)
    with pytest.raises(ValueError, match=f"only {free} of {free + 1}"):
        submit.pick_worlds(pool, free + 1)
    # Once a lineage has had every mechanism (here via its reuses' slots), it is spent.
    spent = [{**w, "lineage_mechanisms": list(dist)} for w in pool]
    with pytest.raises(ValueError, match="only 0 of 1"):
        submit.pick_worlds(spent, 1)


def test_the_task_view_shows_the_reused_world_while_create_runs(client, backend):
    source(backend, "src-1", "compilers_parsers", "w1")
    tid = client.post("/v1/batches", json={"batch_id": "rv", "count": 1, "reuse_worlds": True},
                      headers=AUTH).json()["task_ids"][0]
    d = client.get(f"/v1/tasks/{tid}", headers=AUTH).json()
    # The reuse block names the source world; the version's own world is the variant, not built yet.
    assert d["reuse"]["world_sha256"] == "w1" and d["reuse"]["from_task"] == "src-1"
    assert client.get("/v1/tasks/src-1", headers=AUTH).json()["reuse"] is None
