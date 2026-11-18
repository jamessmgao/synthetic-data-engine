"""scripts/p1_prototype.py against a fake chain: no Harbor, no model, no spend."""

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("p1_prototype", ROOT / "scripts" / "p1_prototype.py")
p1 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p1)


class FakeChain:
    """Scripted job functions. `script` maps a call kind to a list of results returned
    in order (the last one repeats); every call is logged."""

    def __init__(self, **script):
        self.script = {"world": [{"outcome": "OK", "cost_usd": 3.0, "result": {"world_sha256": "w1"}}],
                       "world_check": [{"outcome": "OK"}],
                       "create": [{"outcome": "OK", "cost_usd": 2.0, "result": {"bundle_sha256": "b1"}}],
                       "validate": [{"outcome": "OK", "cost_usd": 0.0}],
                       "rollout": [{"outcome": "VALID_PASS", "cost_usd": 0.02}], **script}
        self.calls: list[tuple] = []

    def _next(self, kind, *args):
        self.calls.append((kind, *args))
        seq = self.script[kind]
        n = sum(c[0] == kind for c in self.calls) - 1
        return dict(seq[min(n, len(seq) - 1)])

    def chain(self):
        return p1.Chain(
            world=lambda jid, inp, work: self._next("world", jid, inp),
            world_check=lambda wdir, tdir: self._next("world_check", wdir.name),
            create=lambda jid, inp, work: self._next("create", jid, inp),
            validate=lambda bdir, tdir: self._next("validate", bdir.name),
            rollout=lambda bdir, jid, tdir: self._next("rollout", jid),
            choose_mutations=lambda passes, rollouts, analysis, history: {
                "operators": ["add_persistent_state"], "why": ["the fix was in memory"], "rules": [],
                "target_fix": "a process-local set"},
        )

    def kinds(self, kind):
        return [c for c in self.calls if c[0] == kind]


def run(tmp_path, fake, *args):
    argv = ["--run", "r", "--tasks", "1", "--work-dir", str(tmp_path / "work"), "--out-dir", str(tmp_path / "out"),
            *args]
    return p1.main(argv, chain=fake.chain())


def ledger(tmp_path):
    return json.loads((tmp_path / "out" / "r" / "ledger.json").read_text())


# --- dry run --------------------------------------------------------------------------------


def test_dry_run_is_the_default_and_calls_nothing(tmp_path, capsys):
    fake = FakeChain()
    assert run(tmp_path, fake, "--tasks", "3") == 0
    assert fake.calls == [] and not (tmp_path / "out").exists()
    out = capsys.readouterr().out
    assert "P1 dry run" in out and "estimated total" in out and "Nothing was called" in out
    for family in p1.P1_FAMILIES:  # three tasks cycle the three P1 families
        assert family in out
    assert "readiness for --execute" in out


def test_execute_needs_a_budget(tmp_path):
    with pytest.raises(SystemExit):
        run(tmp_path, FakeChain(), "--execute")


# --- a full task ---------------------------------------------------------------------------


def test_a_task_runs_the_whole_chain_and_writes_the_summary(tmp_path):
    fake = FakeChain(rollout=[{"outcome": "VALID_PASS", "cost_usd": 0.02}, {"outcome": "VALID_FAIL", "cost_usd": 0.02},
                              {"outcome": "VALID_FAIL", "cost_usd": 0.02}])
    assert run(tmp_path, fake, "--execute", "--budget-usd", "20") == 0
    world_input = fake.kinds("world")[0][2]
    assert world_input["origin"] == "initial" and world_input["spec"]["family"] == p1.P1_FAMILIES[0]
    create_input = fake.kinds("create")[0][2]
    assert create_input == {"origin": "initial", "world_sha256": "w1", "shape": world_input["spec"]["shape"],
                            "dial": world_input["spec"]["dial"]}
    assert len(fake.kinds("rollout")) == 5
    summary = (tmp_path / "out" / "r" / "summary.md").read_text()
    assert "in_band" in summary and "✓✗✗✗✗" in summary and "$5.1000" not in summary
    assert "Spend: **$5.10** of $20.00" in summary  # 3 + 2 + 5 x 0.02
    assert set(ledger(tmp_path)) == {"r-t01-v1-world", "r-t01-v1-world-check-r0", "r-t01-v1-create",
                                     "r-t01-v1-validate-r0", *(f"r-t01-v1-rollout{s}-r0" for s in range(1, 6))}


def test_invalid_rollouts_are_retried_not_scored(tmp_path):
    fake = FakeChain(rollout=[{"outcome": "INVALID", "cost_usd": 0.0, "error": "AgentTimeoutError"},
                              {"outcome": "VALID_PASS", "cost_usd": 0.02}])
    run(tmp_path, fake, "--execute", "--budget-usd", "20")
    ids = [c[1] for c in fake.kinds("rollout")]
    assert ids[:2] == ["r-t01-v1-rollout1-r0", "r-t01-v1-rollout1-r1"] and len(ids) == 6
    assert "too_easy" in (tmp_path / "out" / "r" / "summary.md").read_text()


# --- repairs -------------------------------------------------------------------------------


def test_world_and_task_repairs(tmp_path):
    fake = FakeChain(
        world_check=[{"outcome": "FAILED", "feedback": "repro passes"}, {"outcome": "OK"}],
        world=[{"outcome": "OK", "cost_usd": 3.0, "result": {"world_sha256": "w1"}},
               {"outcome": "OK", "cost_usd": 3.0, "result": {"world_sha256": "w2"}}],
        create=[{"outcome": "FAILED", "cost_usd": 2.0, "error": "leak",
                 "result": {"failure_kind": "author", "bundle_sha256": "b1", "feedback": "instruction leaks"}},
                {"outcome": "OK", "cost_usd": 2.0, "result": {"bundle_sha256": "b2"}},
                {"outcome": "OK", "cost_usd": 2.0, "result": {"bundle_sha256": "b3"}}],
        validate=[{"outcome": "FAILED", "feedback": "nop passes"}, {"outcome": "OK"}],
    )
    run(tmp_path, fake, "--execute", "--budget-usd", "30")
    worlds = fake.kinds("world")
    assert worlds[1][1] == "r-t01-v1-world-repair1" and worlds[1][2]["origin"] == "repair"
    assert worlds[1][2]["feedback"] == "repro passes" and worlds[1][2]["world_sha256"] == "w1"
    creates = fake.kinds("create")
    assert [c[1] for c in creates] == ["r-t01-v1-create", "r-t01-v1-repair1", "r-t01-v1-repair2"]
    assert creates[1][2] == {"origin": "repair", "bundle_sha256": "b1", "world_sha256": "w2",
                             "feedback": "instruction leaks"}  # the leak, repaired
    assert creates[2][2]["feedback"] == "nop passes" and creates[2][2]["bundle_sha256"] == "b2"  # the gate
    assert [c[1] for c in fake.kinds("validate")] == ["b2", "b3"]


def test_an_infra_create_failure_ends_the_task(tmp_path):
    fake = FakeChain(create=[{"outcome": "FAILED", "error": "AgentTimeoutError", "result": {"failure_kind": "infra"}}])
    run(tmp_path, fake, "--execute", "--budget-usd", "20")
    assert len(fake.kinds("create")) == 1 and fake.kinds("rollout") == []
    assert "invalid_task" in (tmp_path / "out" / "r" / "summary.md").read_text()


# --- budget and resume ---------------------------------------------------------------------


def test_budget_stops_before_a_step_that_would_exceed_it(tmp_path):
    fake = FakeChain()
    assert run(tmp_path, fake, "--execute", "--budget-usd", "1") == 2  # the world alone is estimated at $3
    assert fake.calls == []
    assert "would take the spend from $0.00 past the $1.00 budget" in (tmp_path / "out" / "r" / "summary.md").read_text()


def test_budget_uses_actual_spend(tmp_path):
    # The world actually cost $4.5; the create (est. $2.5) would then pass $6.
    fake = FakeChain(world=[{"outcome": "OK", "cost_usd": 4.5, "result": {"world_sha256": "w1"}}])
    assert run(tmp_path, fake, "--execute", "--budget-usd", "6") == 2
    assert [c[0] for c in fake.calls] == ["world", "world_check"]
    summary = (tmp_path / "out" / "r" / "summary.md").read_text()
    assert "r-t01-v1-create (create, est. $2.50) would take the spend from $4.50" in summary
    assert "r-t01" in summary  # the partial task is in the summary


def test_a_rerun_resumes_without_repeating_finished_steps(tmp_path):
    first = FakeChain()
    assert run(tmp_path, first, "--execute", "--budget-usd", "5") == 2  # $3 spent + create est. $2.50 > $5
    second = FakeChain()
    assert run(tmp_path, second, "--execute", "--budget-usd", "20") == 0
    assert second.kinds("world") == [] and second.kinds("world_check") == []  # adopted from the ledger
    assert len(second.kinds("create")) == 1 and len(second.kinds("rollout")) == 5
    assert "Spend: **$5.10**" in (tmp_path / "out" / "r" / "summary.md").read_text()  # each step counted once


# --- the mutation round --------------------------------------------------------------------


def test_a_too_easy_task_gets_one_mutation_round(tmp_path):
    fake = FakeChain(world=[{"outcome": "OK", "cost_usd": 3.0, "result": {"world_sha256": "w1"}},
                            {"outcome": "OK", "cost_usd": 3.0, "result": {"world_sha256": "w2"}}])
    run(tmp_path, fake, "--execute", "--budget-usd", "30", "--mutation")
    v2 = fake.kinds("world")[1]
    assert v2[1] == "r-t01-v2-world"
    inp = v2[2]
    assert inp["origin"] == "mutation" and inp["parent_world_sha256"] == "w1"
    assert inp["mutation"] == {"operators": ["add_persistent_state"], "why": "the fix was in memory"}
    assert inp["target_fix"] == "a process-local set" and inp["mutations"][-1]["operators"] == ["add_persistent_state"]
    assert len(fake.kinds("rollout")) == 10  # 5 for v1, 5 for v2
    summary = (tmp_path / "out" / "r" / "summary.md").read_text()
    assert "| v1 | too_easy" in summary and "| v2 | too_easy" in summary


def test_no_mutation_round_without_the_flag_or_when_in_band(tmp_path):
    fake = FakeChain()
    run(tmp_path, fake, "--execute", "--budget-usd", "30")
    assert len(fake.kinds("world")) == 1
