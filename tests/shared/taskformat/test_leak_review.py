"""The semantic leak check (leak.gate_leak) on P1's real task: its instruction, its
world and its fix (tests/shared/taskformat/fixtures/p1, from origin/task-builder 2f9d456). The
model calls are scripted (no spend); what is tested is what each call is shown, how
findings are verified and graded, and the verdict rules."""

import json
from pathlib import Path

import pytest

from shared.llm import llm
from shared.taskformat import leak

HERE = Path(__file__).resolve().parent

P1 = HERE / "fixtures" / "p1"
P1_INSTRUCTION = (P1 / "instruction.md").read_text()
# The GOOD example from task_author.md: the same guarantees, no hook, no schedule, no fix.
GOOD_INSTRUCTION = """## Durable reservation service

You are working in `/app` on the `reservation` command-line service. Clients submit stable request IDs such as `order-1`, and delivery can be retried.

A worker process can be killed at any moment (including in the middle of a write) and a new one started. Time advances only through `tick --milliseconds N`; a lease lasts 30000 ms.

Users report that stock sometimes falls by more than a request should consume after retries, and later requests that should fit are rejected.

Each request ID consumes its submitted quantity at most once, whatever the retries, crashes and lease expiries; stock never goes negative; and a request that fits the stock is eventually fulfilled.
"""

# P1's sentences a reviewer should flag (they are in its instruction verbatim).
HOOK = ("The documented fault-injection hook is `STOP_AFTER=inventory`: when a claimed request can be fulfilled, at "
        "that point a worker exits with status `70` after the accepted inventory movement and stock update are "
        "durable and before the worker’s remaining completion work.")
FIX = ("Once a request ID has produced an inventory movement, any later delivery of that same request ID must reuse "
       "that exact durable result without applying another inventory effect.")
assert HOOK in P1_INSTRUCTION and FIX in P1_INSTRUCTION


def bundle(tmp_path: Path, instruction: str) -> Path:
    """A content-addressed bundle in the pipeline's layout, written from P1's world."""
    work = tmp_path / "work"
    world = work / "worlds" / "w-p1"
    (world / "world_fix").mkdir(parents=True)
    (world / "world.json").write_text((P1 / "world.json").read_text())
    (world / "world_fix" / "fix.sh").write_text((P1 / "fix.sh").read_text())
    b = work / "bundles" / "b-p1"
    (b / "solution").mkdir(parents=True)
    (b / "README.md").write_text((P1 / "README.md").read_text())
    (b / "instruction.md").write_text(instruction)
    (b / "solution" / "solve.sh").write_text("cp /solution/inventory.py /app/reservation/inventory.py\n")
    (b / "solution" / "wrong_fix.sh").write_text("# plausible_wrong_fix: a process-local set\n")
    (b / "task.toml").write_text('[metadata.generation]\nworld_sha256 = "w-p1"\n')
    return b


class Model:
    """Scripted replies per call (cache_key), each costing 0.02; records what each call saw."""

    def __init__(self, review: dict, judge: str = "none", prediction: str = "unclear from the instruction"):
        self.replies = {"leak-review": review,
                        "leak-blind": {"predicted_fix": prediction, "predicted_failure": "unclear"},
                        "leak-judge": {"match": judge, "why": "the prediction names the mechanism and place"}}
        self.seen: dict[str, str] = {}

    def __call__(self, messages, response_format, llm_dir=None, cache_key=None, **_):
        self.seen[cache_key] = messages[1]["content"]
        return llm.Completion(content=json.dumps(self.replies[cache_key]), finish_reason="stop", model="fake",
                              prompt_tokens=1, completion_tokens=1, reasoning_tokens=0, gateway_cost_usd=None,
                              cost_usd=0.02, duration_sec=0.1, transport_retries=0,
                              request_sha256=f"sha-{cache_key}", cached=False)


@pytest.fixture(autouse=True)
def review_on(monkeypatch):
    monkeypatch.setattr(leak.config, "LEAK_REVIEW", True)


def run(tmp_path, monkeypatch, instruction, model):
    monkeypatch.setattr(llm, "complete", model)
    return leak.gate_leak(bundle(tmp_path, instruction), tmp_path / "trace")


def finding(quote, severity="direct", reveals="the fix"):
    return {"quote": quote, "reveals": reveals, "severity": severity}


# --- P1 is flagged by each check --------------------------------------------------------


def test_p1_is_flagged_by_the_semantic_review(tmp_path, monkeypatch):
    model = Model({"verdict": "leak", "leaks": [finding(HOOK, reveals="where the crash lands"),
                                                finding(FIX, reveals="idempotency keyed on the request ID")]})
    gate = run(tmp_path, monkeypatch, P1_INSTRUCTION, model)
    assert not gate.passed and gate.detail["direct"] == 2 and not gate.detail["blind_leak"]
    assert "idempotency keyed on the request ID" in gate.feedback and "crash hooks or stage names" in gate.feedback


def test_p1_is_flagged_by_the_blind_prediction(tmp_path, monkeypatch):
    model = Model({"verdict": "clean", "leaks": []}, judge="specific",
                  prediction="make the inventory debit idempotent on the request ID and reuse the recorded movement")
    gate = run(tmp_path, monkeypatch, P1_INSTRUCTION, model)
    assert not gate.passed and gate.detail["blind_leak"] and gate.detail["direct"] == 0
    assert "saw ONLY the instruction predicted the real fix" in gate.feedback
    assert "idempotent on the request ID" in gate.feedback


def test_the_good_instruction_passes(tmp_path, monkeypatch):
    # A reviewer that quotes P1's sentences finds them absent here; the blind prediction is generic.
    model = Model({"verdict": "leak", "leaks": [finding(HOOK), finding(FIX)]}, judge="generic",
                  prediction="make processing idempotent and transactional")
    gate = run(tmp_path, monkeypatch, GOOD_INSTRUCTION, model)
    assert gate.passed, gate.feedback
    assert len(gate.detail["unverified_quotes"]) == 2 and gate.detail["direct"] == 0


# --- what each call sees ------------------------------------------------------------------


def test_the_reviewer_sees_the_world_and_the_solution_the_blind_predictor_only_the_instruction(tmp_path,
                                                                                            monkeypatch):
    model = Model({"verdict": "clean", "leaks": []})
    gate = run(tmp_path, monkeypatch, P1_INSTRUCTION, model)
    review, blind, judge = model.seen["leak-review"], model.seen["leak-blind"], model.seen["leak-judge"]
    world = json.loads((P1 / "world.json").read_text())
    for part in ("HIDDEN AUTHOR NOTES", "THE WORLD", world["counterexample"]["sequence"][2],
                 world["plausible_wrong_fix"], "The real fix (world_fix/fix.sh)", "REFERENCE SOLUTION",
                 "solution/solve.sh", P1_INSTRUCTION.strip()[:200]):
        assert part in review, part
    assert "wrong_fix.sh" not in review.split("REFERENCE SOLUTION")[1].split("INSTRUCTION SHOWN")[0]
    assert blind == f"INSTRUCTION\n\n{P1_INSTRUCTION}"  # nothing hidden
    assert "THE WORLD" in judge and "PREDICTION FROM THE INSTRUCTION ALONE" in judge
    assert gate.detail["had_world"]
    assert [c["call"] for c in gate.detail["calls"]] == ["leak-review", "leak-blind", "leak-judge"]
    assert gate.detail["cost_usd"] == pytest.approx(0.06)


# --- grading and verification --------------------------------------------------------------


@pytest.mark.parametrize("severities,leak", [(["strong_hint"], False), (["strong_hint", "strong_hint"], True),
                                             (["direct"], True), ([None], True)])  # ungraded counts as direct
def test_one_strong_hint_is_tolerated_two_are_a_leak(tmp_path, monkeypatch, severities, leak):
    quotes = [HOOK, FIX]
    leaks = [{"quote": q, "reveals": "x", **({"severity": s} if s else {})} for q, s in zip(quotes, severities)]
    gate = run(tmp_path, monkeypatch, P1_INSTRUCTION, Model({"verdict": "leak", "leaks": leaks}))
    assert gate.passed is not leak


def test_a_paraphrased_quote_is_not_verified(tmp_path, monkeypatch):
    """The finding must quote the instruction; the paraphrase is in what it reveals."""
    gate = run(tmp_path, monkeypatch, P1_INSTRUCTION, Model({"verdict": "leak", "leaks": [
        finding("later deliveries of a request ID reuse its durable result")]}))
    assert gate.passed and gate.detail["unverified_quotes"]


def test_the_review_is_adopted_and_an_old_record_is_reviewed_again(tmp_path, monkeypatch):
    b = bundle(tmp_path, P1_INSTRUCTION)
    model = Model({"verdict": "clean", "leaks": []})
    monkeypatch.setattr(llm, "complete", model)
    record = leak.leak_review_dir(b, tmp_path / "trace") / "review.json"
    record.parent.mkdir(parents=True)
    record.write_text(json.dumps({"review": {"verdict": "clean", "leaks": []}, "cost_usd": 0.01}))  # v1
    first = leak.gate_leak(b, tmp_path / "trace")
    assert first.detail["adopted"] is False and len(model.seen) == 3  # a v1 record has no blind check
    model.seen.clear()
    again = leak.gate_leak(b, tmp_path / "trace")
    assert again.detail["adopted"] and again.detail["cost_usd"] == 0.0 and not model.seen


def test_the_blind_check_can_be_switched_off(tmp_path, monkeypatch):
    monkeypatch.setattr(leak.config, "LEAK_BLIND", False)
    model = Model({"verdict": "leak", "leaks": [finding(FIX)]}, judge="specific")
    gate = run(tmp_path, monkeypatch, P1_INSTRUCTION, model)
    assert not gate.passed and set(model.seen) == {"leak-review"} and not gate.detail["blind_leak"]
    assert gate.detail["cost_usd"] == pytest.approx(0.02)


def test_a_mutated_world_tells_the_judge_the_parent_fix_is_known(tmp_path, monkeypatch):
    b = bundle(tmp_path, GOOD_INSTRUCTION)
    world_json = tmp_path / "work" / "worlds" / "w-p1" / "world.json"
    meta = json.loads(world_json.read_text())
    meta["lineage"] = {"mutations": [{"defeats": "commit-before-ack now also promotes a rejected over-limit batch"}]}
    world_json.write_text(json.dumps(meta))
    model = Model({"verdict": "clean", "leaks": []})
    run_gate = leak.gate_leak
    monkeypatch.setattr(llm, "complete", model)
    run_gate(b, tmp_path / "trace")
    judge = model.seen["leak-judge"]
    assert "mutation of a solved parent" in judge and "promotes a rejected over-limit batch" in judge
    assert "mutation of a solved parent" not in model.seen["leak-blind"]  # the predictor still sees only the instruction


def test_without_the_world_the_review_still_runs_on_the_bundle(tmp_path, monkeypatch):
    b = bundle(tmp_path, P1_INSTRUCTION)
    (b / "task.toml").write_text("")  # no world recorded
    model = Model({"verdict": "clean", "leaks": []})
    monkeypatch.setattr(llm, "complete", model)
    gate = leak.gate_leak(b, tmp_path / "trace")
    assert gate.passed and not gate.detail["had_world"] and "REFERENCE SOLUTION" in model.seen["leak-review"]
