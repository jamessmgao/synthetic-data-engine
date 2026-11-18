"""shared/taxonomy/world_selection.py: which checked world a reuse_worlds task gets."""
import json

from shared.taxonomy.world_selection import Use, World, domain, select_worlds, tier

CC, DB = "cross_component_stateful", "database_consistency"


def w(sha, family=CC, mechanism="m1", uses=(), reuses=0, failed=False, name=""):
    return World(sha, family, mechanism, list(uses), reuses, failed, name)


def test_domains_from_world_names():
    assert domain("durable-quorum-ledger") == "replication/consensus"
    assert domain("capacity-prerequisite-planner") == "scheduling/planning"
    assert domain("quota-charge-retry") == "allocation/provisioning"
    assert domain("sqlite-charge-recovery") == "ledger/payments"
    assert domain("shadowed-let-inliner") == "compiler/interpreter"
    assert domain("reservation-dispatch-recovery") == "booking/reservation"
    assert domain("mystery-box") == domain("") == "other"


def test_repeats_of_a_live_scenario_go_after_novel_worlds_and_rare_domains_go_first():
    worlds = [w("ledger-live", uses=[Use(pass_count=5, live=True, task="t1"), Use(pass_count=5, task="t2")],
                name="payment-settlement-recovery"),  # its own task is live
              w("ledger-2", uses=[Use(pass_count=4)], name="charge-ledger-retry"),  # repeats ledger + m1
              w("ledger-m2", mechanism="m2", uses=[Use(pass_count=4)], name="ledger-dedup-cache"),
              w("booking", mechanism="m3", uses=[Use(pass_count=4)], name="booking-isolation")]
    picks = select_worlds(worlds, [CC], 4)
    # ledger/payments already has a live task, so booking goes first; then the ledger world on
    # another mechanism; then the repeats (ledger-2, then the live world itself).
    assert [p.world_sha256 for p in picks] == ["booking", "ledger-m2", "ledger-2", "ledger-live"]
    assert "domain booking/reservation" in picks[0].reason
    assert "repeats a live ledger/payments + m1 scenario" in picks[2].reason
    assert "its own task is live (the variant takes another mechanism)" in picks[3].reason


def test_blind_leak_and_greedy_worlds_are_never_reused():
    blind = w("blind", mechanism="m2", uses=[Use(pass_count=4), Use(leak_rejected=True, new_prompts=False,
                                                                     blind_leak=True)])
    greedy = World("greedy", CC, "m3", [Use(pass_count=4)], greedy=True)
    easy = w("easy", mechanism="m4", uses=[Use(pass_count=5)])
    assert tier(blind) == (None, "a task on it was rejected for a leak only the blind predictor found")
    assert tier(greedy)[0] is None
    # aws-w3-l2-t01: a greedy world put last still got picked, and leaked ×3.
    assert [p.world_sha256 for p in select_worlds([blind, greedy, easy], [CC], 3)] == ["easy"]


def test_greedy_defect_reads_the_wrong_fix_and_counterexample(tmp_path):
    from shared.taxonomy.world_selection import greedy_defect, greedy_worlds
    assert greedy_defect({"plausible_wrong_fix": "Keep the first-fit packer but sort by size first",
                          "counterexample": {"observed": "3 bins"}})
    assert greedy_defect({"counterexample": {"why": "the greedy choice of the longest job loses"}})
    assert not greedy_defect({"plausible_wrong_fix": "Check the lease in Worker.commit", "counterexample": {}})

    class Store:  # the artifact store's fetch_file / root interface
        root = tmp_path

        def fetch_file(self, rel):
            return (tmp_path / rel).is_file()

    for sha, wrong in (("g1", "a greedy pass over the jobs"), ("x1", "retry the commit")):
        (tmp_path / "worlds" / sha).mkdir(parents=True)
        (tmp_path / "worlds" / sha / "world.json").write_text(json.dumps({"plausible_wrong_fix": wrong}))
    (tmp_path / "worlds" / "bad").mkdir()
    (tmp_path / "worlds" / "bad" / "world.json").write_text("{not json")
    assert greedy_worlds(Store(), ["g1", "x1", "bad", "missing"]) == {"g1"}


def test_a_world_with_a_live_task_serves_after_the_idle_ones():
    live = w("live", uses=[Use(live=True, task="t1")], name="zebra-crossing")
    idle = w("idle", mechanism="m2", uses=[Use(pass_count=5, task="t3")], name="zebra-crossing")
    picks = select_worlds([live, idle], [CC], 2)
    assert [p.world_sha256 for p in picks] == ["idle", "live"]
    assert "its own task is live" in picks[1].reason
    assert domain("bank-write-skew") == "ledger/payments"


def test_a_second_world_of_a_scenario_picked_in_the_same_call_comes_after():
    worlds = [w("a", uses=[Use(pass_count=4)], name="durable-quorum-ledger"),
              w("b", uses=[Use(pass_count=4)], name="replicated-kv-linearizability"),
              w("c", mechanism="m9", uses=[Use(pass_count=5)], name="booking-isolation")]
    # b repeats a's replication + m1 once a is picked, so c (a 5/5 world) goes before it.
    assert [p.world_sha256 for p in select_worlds(worlds, [CC], 3)] == ["a", "c", "b"]


def test_new_prompt_leaks_failed_and_exhausted_worlds_are_excluded_accepted_and_reused_are_not():
    # A variant takes a mechanism its lineage hasn't had (the old defect removed), so an accepted
    # task or earlier reuses don't make it a repeat; running out of mechanisms does.
    assert tier(w("a", uses=[Use(pass_count=2, accepted=True)]))[0] is not None
    assert tier(w("d", uses=[Use(pass_count=4)], reuses=5))[0] == "near"
    assert tier(w("b", uses=[Use(leak_rejected=True, new_prompts=True)]))[0] is None
    assert tier(w("c", failed=True))[0] is None
    spent = World("e", CC, "m1", [Use(pass_count=4)], free_mechanisms=0)
    assert tier(spent) == (None, "every usable mechanism of its family already has a variant in its lineage")


def test_a_lineage_serves_once_per_free_mechanism_in_one_call():
    two = World("two", CC, "m1", [Use(pass_count=4)], lineage="L", free_mechanisms=2)
    other = World("other", CC, "m2", [Use(pass_count=5)], lineage="M", free_mechanisms=1, name="booking-isolation")
    # A lineage not yet picked goes first; "two" then serves once more (its second free mechanism).
    assert [p.world_sha256 for p in select_worlds([two, other], [CC], 4)] == ["two", "other", "two"]


def test_tiers_follow_the_policy():
    assert tier(w("a", uses=[Use(pass_count=5), Use(pass_count=4)]))[0] == "near"
    assert tier(w("b", uses=[Use(pass_count=0)]))[0] == "hard"
    assert tier(w("c", uses=[Use()]))[0] == "unscored"
    assert tier(w("d", uses=[Use(leak_rejected=True, new_prompts=False)]))[0] == "old_leak"
    assert tier(w("e", uses=[Use(pass_count=5), Use(pass_count=5)]))[0] == "easy"


def test_better_tiers_are_picked_first_within_a_family():
    worlds = [w("easy", uses=[Use(pass_count=5)]), w("near", uses=[Use(pass_count=4)]),
              w("hard", uses=[Use(pass_count=0)])]
    picks = select_worlds(worlds, [CC], 3)
    assert [(p.world_sha256, p.tier) for p in picks] == [("near", "near"), ("hard", "hard"), ("easy", "easy")]
    assert "4/5" in picks[0].reason


def test_families_alternate_and_mechanisms_spread():
    worlds = [w("cc1", CC, "m1", [Use(pass_count=4)]), w("cc2", CC, "m1", [Use(pass_count=4)]),
              w("cc3", CC, "m2", [Use(pass_count=4)]), w("db1", DB, "x", [Use(pass_count=4)])]
    picks = select_worlds(worlds, [CC, DB], 3)
    assert [p.family for p in picks] == [CC, DB, CC]
    # m2 has fewer uses than m1 (m1 has two worlds), so the first cc pick is m2's world
    assert picks[0].world_sha256 == "cc3" and picks[2].mechanism == "m1"


def test_other_families_are_ignored_and_a_short_pool_returns_fewer():
    worlds = [w("perf", "performance_resources", uses=[Use(pass_count=4)]), w("cc", CC, uses=[Use(pass_count=4)])]
    picks = select_worlds(worlds, [CC], 5)
    assert [p.world_sha256 for p in picks] == ["cc"]


def test_a_world_is_never_picked_twice_in_one_call():
    picks = select_worlds([w("only", uses=[Use(pass_count=4)])], [CC, CC], 2)
    assert [p.world_sha256 for p in picks] == ["only"]
