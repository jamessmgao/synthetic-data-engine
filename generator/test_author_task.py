"""author_task: run_create_job against a fake authoring harness (no Harbor, no model)."""

import json
import re
from pathlib import Path

import pytest

from generator import author_task as at
from test_generator import VALID, WRONG_FIX, draft_with
from generator.worker import load_draft_record

WORLD = {
    "spec": {"family": "database_consistency", "mechanism": "write_skew", "shape": "fix",
             "environment": "Python service (SQLite)", "verification": "invariant_check", "dial": 2},
    "invariants": ["confirmed reservations never exceed stock", "a retried reservation is confirmed once"],
    "counterexample": {"sequence": ["A reads stock=1", "B reads stock=1", "A confirms", "B confirms"],
                       "reproduce": "repro/repro.sh", "observed": "2 confirmed, stock 1"},
    "plausible_wrong_fix": "check-then-insert inside one transaction at READ COMMITTED",
    "lineage": {"parent_world_sha256": None, "mutations": []},
}
GOOD = draft_with(wrong_fix_script=WRONG_FIX)


def make_world(work: Path, sha: str = "w1", world: dict | None = None) -> Path:
    d = work / "worlds" / sha
    (d / "environment").mkdir(parents=True)
    (d / "environment" / "Dockerfile").write_text("FROM python:3.12-slim-bookworm\nWORKDIR /app\n")
    (d / "repro").mkdir()
    (d / "repro" / "repro.sh").write_text("#!/bin/bash\nexit 1\n")
    (d / "world_fix").mkdir()
    (d / "world_fix" / "fix.sh").write_text("#!/bin/bash\ntrue\n")
    (d / "world.json").write_text(json.dumps(world or WORLD))
    return d


class FakeHarness:
    """Stands in for authoring_harness.run_authoring_trial: records the call and writes
    the given draft (or nothing) as the trial's /logs/artifacts/draft.json."""

    def __init__(self, draft: dict | str | None = GOOD, exception: str | None = None, no_trial: bool = False):
        self.draft, self.exception, self.no_trial, self.calls = draft, exception, no_trial, []

    def __call__(self, job_id, instruction, files, work_dir, timeout_sec=3600):
        self.calls.append({"job_id": job_id, "instruction": instruction, "files": files, "timeout_sec": timeout_sec})
        if self.no_trial:
            return {"trial_dir": None, "artifacts_dir": None, "cost_usd": 0.0, "exception_type": "NoTrialResult",
                    "error": "Harbor wrote no trial result", "adopted": False}
        artifacts = Path(work_dir) / "traces" / job_id / "authoring" / "t1" / "artifacts"
        artifacts.mkdir(parents=True, exist_ok=True)
        if self.draft is not None:
            text = self.draft if isinstance(self.draft, str) else json.dumps(self.draft)
            (artifacts / "draft.json").write_text(text)
        return {"trial_dir": str(artifacts.parent), "artifacts_dir": str(artifacts), "cost_usd": 1.25,
                "gateway_cost_usd": 5e-05, "steps": 40, "duration_sec": 1200.0, "n_input_tokens": 800_000,
                "n_output_tokens": 30_000, "adopted": False, "exception_type": self.exception,
                "error": f"{self.exception}: boom" if self.exception else None}


@pytest.fixture
def work(tmp_path):
    make_world(tmp_path)
    return tmp_path


@pytest.fixture(autouse=True)
def no_model_calls(monkeypatch):
    """No test may reach the real model: any unstubbed llm.complete fails the test."""
    from shared.llm import llm

    def refuse(*a, **k):
        raise AssertionError("a test reached the real model (llm.complete)")

    monkeypatch.setattr(llm, "complete", refuse)


class FakeGate:
    def __init__(self, passed=True, feedback="", cost=0.0):
        self.name, self.passed, self.feedback = "leak", passed, feedback
        self.detail = {"verdict": "clean" if passed else "leak", "cost_usd": cost}


def clean_review(bundle_dir, trace_dir):
    return FakeGate()


def create(work, harness=None, leak_review=clean_review, **job_input):
    harness = harness or FakeHarness()
    job_input = {"origin": "initial", "world_sha256": "w1", "shape": "fix", "dial": 2, **job_input}
    return at.run_create_job("t-1-v1-create", job_input, work, run_trial=harness, leak_review=leak_review), harness


# --- the initial create ------------------------------------------------------------------


def test_initial_create_writes_a_bundle_from_the_world(work):
    out, harness = create(work)
    assert out["outcome"] == "OK" and out["error"] is None and out["cost_usd"] == 1.25
    r = out["result"]
    assert r["world_sha256"] == "w1" and Path(r["bundle_path"]).is_dir()
    assert (Path(r["bundle_path"]) / "solution" / "wrong_fix.sh").is_file()  # TB1 materializes it
    assert r["authoring"]["steps"] == 40 and r["authoring"]["adopted"] is False
    meta = load_draft_record(r["bundle_sha256"], work)["generation_meta"]
    assert meta | {"prompt_sha256": "x"} == {
        "model": meta["model"], "author_mode": "agent", "origin": "initial", "job_id": "t-1-v1-create",
        "world_sha256": "w1", "shape": "fix", "dial": 2, "prompt_sha256": "x"}
    files = harness.calls[0]["files"]
    assert set(files) == {"world/world.json", "world/environment/Dockerfile", "world/repro/repro.sh",
                          "world/world_fix/fix.sh"}
    assert harness.calls[0]["timeout_sec"] == 3600


def test_instruction_carries_the_world_the_shape_and_the_rules(work):
    _, harness = create(work, shape="implement", dial=3)
    text = harness.calls[0]["instruction"]
    for needle in ("confirmed reservations never exceed stock", "A reads stock=1", "2 confirmed, stock 1",
                   "check-then-insert inside one transaction", "/input/world/", "/logs/artifacts/draft.json",
                   "sde-task check /logs/artifacts/draft.json", "--as oracle", "--as nop", "--as wrong",
                   "Shape: implement", "level 3 of 3", at.FORMS["many_rules"]["dial"][3],
                   "Mistakes earlier drafts made", "python:3.12-slim-bookworm",  # system.md, images filled in
                   "# plausible_wrong_fix: <the plausible_wrong_fix text>", "setup_errors",
                   "the test that replays the world's counterexample MUST be among the failures"):
        assert needle in text, needle
    assert re.findall(r"\$\w+", text) == ["$SDE_DOCKER_LABEL"]  # every template slot filled
    assert "{base_images}" not in text and "This is a revision" not in text


def test_every_shape_and_dial_renders(work):
    for shape in at.SHAPES:
        for dial in (0, 1, 2, 3, 9):  # clamped to 1-3
            section = at.shape_section(shape, dial)
            assert f"Shape: {shape}." in section and "level" in section
    assert at.SHAPES["fix"]["dial"] == at.FORMS["behavior_preserving"]["dial"]


def test_rerunning_the_job_gives_the_same_bundle(work):
    first, _ = create(work)
    again, _ = create(work)
    assert first["result"]["bundle_sha256"] == again["result"]["bundle_sha256"]


# --- failures: whose fault ----------------------------------------------------------------


@pytest.mark.parametrize("harness, needle", [
    (FakeHarness(draft=None, exception="AgentTimeoutError"), "AgentTimeoutError"),
    (FakeHarness(no_trial=True), "no trial result"),
])
def test_a_failed_trial_without_a_draft_is_infra(work, harness, needle):
    out, _ = create(work, harness)
    assert (out["outcome"], out["result"]["failure_kind"]) == ("FAILED", "infra") and needle in out["error"]


def test_a_valid_draft_is_kept_when_the_trial_fails_after_writing_it(work):
    out, _ = create(work, FakeHarness(exception="AgentTimeoutError"))
    assert out["outcome"] == "OK" and out["result"]["authoring"]["exception_type"] == "AgentTimeoutError"


def test_a_missing_world_is_infra(work):
    out, harness = create(work, world_sha256="nope")
    assert out["result"]["failure_kind"] == "infra" and "world nope unavailable" in out["error"]
    assert harness.calls == []  # nothing paid for


@pytest.mark.parametrize("draft, needle", [
    (None, "wrote no /logs/artifacts/draft.json"),
    ("{not json", "not a valid TaskDraft"),
    (draft_with(name="X"), "not a valid TaskDraft"),
    (VALID, "no wrong_fix_script"),
])
def test_a_missing_or_bad_draft_is_the_authors_fault(work, draft, needle):
    out, _ = create(work, FakeHarness(draft=draft))
    assert (out["outcome"], out["result"]["failure_kind"]) == ("FAILED", "author") and needle in out["error"]
    assert out["cost_usd"] == 1.25 and out["result"]["authoring"]["steps"] == 40  # spend is still recorded


@pytest.mark.parametrize("job_input, needle", [
    ({"origin": "nonsense"}, "unknown origin"),
    ({"shape": "rewrite"}, "unknown shape"),
])
def test_bad_job_input_is_rejected_before_paying(work, job_input, needle):
    out, harness = create(work, **job_input)
    assert out["result"]["failure_kind"] == "author" and needle in out["error"] and harness.calls == []


# --- revisions ---------------------------------------------------------------------------


@pytest.fixture
def parent(work):
    out, _ = create(work)
    return out["result"]["bundle_sha256"]


def revise(work, harness=None, **job_input):
    harness = harness or FakeHarness(draft=draft_with(wrong_fix_script=WRONG_FIX, description="revised"))
    return at.run_create_job("t-1-v2-revise", job_input, work, run_trial=harness, leak_review=clean_review), harness


def test_repair_carries_the_gate_feedback_and_the_previous_draft(work, parent):
    out, harness = revise(work, origin="repair", bundle_sha256=parent, feedback="wrong_fix: the tests accept it")
    assert out["outcome"] == "OK", out["error"]
    call = harness.calls[0]
    assert json.loads(call["files"]["previous/draft.json"])["name"] == GOOD["name"]
    assert "This is a revision" in call["instruction"] and "the tests accept it" in call["instruction"]
    meta = load_draft_record(out["result"]["bundle_sha256"], work)["generation_meta"]
    # world, shape and dial come from the parent when the revise input omits them
    assert (meta["origin"], meta["parent_bundle_sha256"], meta["world_sha256"], meta["shape"]) == (
        "repair", parent, "w1", "fix")


def test_easier_and_repair_ambiguity(work, parent):
    out, harness = revise(work, origin="easier", bundle_sha256=parent, world_sha256="w1",
                          result="0 of 5 runs passed.", evidence="- run 1: failed: missed the retry")
    text = harness.calls[0]["instruction"]
    assert out["outcome"] == "OK" and "0 of 5 runs passed." in text and "missed the retry" in text
    assert "The world stays the same" in text
    out, harness = revise(work, origin="repair_ambiguity", bundle_sha256=parent, world_sha256="w1",
                          evidence="the tests require port 9000, the instruction never says so")
    assert out["outcome"] == "OK" and "port 9000" in harness.calls[0]["instruction"]


def test_one_shot_style_revision_maps_to_an_agent_origin(work, parent):
    out, harness = revise(work, origin="revision", direction="repair", bundle_sha256=parent, evidence="ambiguous")
    assert out["outcome"] == "OK" and "the task itself is at fault" in harness.calls[0]["instruction"]


def test_a_revision_may_not_drop_tests(work, parent):
    # A valid draft, but test_even_unsorted is gone (renamed): the repair guard must catch it.
    dropped = draft_with(
        wrong_fix_script=WRONG_FIX,
        test_outputs=VALID["test_outputs"].replace("def test_even_unsorted", "def test_even_renamed"),
        coverage=[{"requirement_id": "C1", "tests": ["test_odd"]},
                  {"requirement_id": "C2", "tests": ["test_even", "test_even_renamed"]}])
    out, _ = revise(work, FakeHarness(draft=dropped), origin="repair", bundle_sha256=parent, feedback="x")
    assert out["result"]["failure_kind"] == "author" and "dropped tests or constraints" in out["error"]


def test_a_revision_without_its_parent_draft_is_infra(work):
    out, harness = revise(work, origin="repair", bundle_sha256="missing", feedback="x")
    assert out["result"]["failure_kind"] == "infra" and harness.calls == []


# --- the leak review moves into the create job (plan N6) -----------------------------------


def fake_llm(monkeypatch, verdict="clean", leaks=(), cost=0.03):
    """Stub llm.complete for the real gate_leak; returns the list of calls."""
    from shared.llm import llm

    calls = []

    def complete(messages, response_format, llm_dir=None, cache_key=None):
        calls.append({"llm_dir": llm_dir, "user": messages[1]["content"]})
        return llm.Completion(content=json.dumps({"verdict": verdict, "leaks": list(leaks)}), finish_reason="stop",
                              model="z-ai/glm-5.1", prompt_tokens=1, completion_tokens=1, reasoning_tokens=0,
                              gateway_cost_usd=None, cost_usd=cost, duration_sec=1.0, transport_retries=0,
                              request_sha256="r1", cached=False)

    monkeypatch.setattr(llm, "complete", complete)
    return calls


def test_create_runs_the_leak_review_and_validate_adopts_it(work, monkeypatch):
    calls = fake_llm(monkeypatch)
    out, _ = create(work, leak_review=None)  # the real gate_leak
    assert out["outcome"] == "OK" and out["cost_usd"] == pytest.approx(1.25 + 0.03)
    assert out["result"]["leak"]["passed"] and out["result"]["leak"]["adopted"] is False and len(calls) == 1
    sha = out["result"]["bundle_sha256"]
    assert (work / "leak_reviews" / sha / "review.json").is_file()
    from validator import gates

    gate = gates.gate_leak(Path(out["result"]["bundle_path"]), work / "traces" / "t-1-v1-validate-r0")
    assert gate.passed and gate.detail["adopted"] is True and gate.detail["cost_usd"] == 0.0
    assert len(calls) == 1  # validate did not call the model again


def test_a_leak_found_at_create_is_the_authors_fault_with_feedback(work, monkeypatch):
    instruction = VALID["instruction"]
    quote = instruction.split(".")[0].strip()  # a real sentence of the instruction
    fake_llm(monkeypatch, verdict="leak", leaks=[{"quote": quote, "reveals": "the fix"}])
    out, _ = create(work, leak_review=None)
    r = out["result"]
    assert (out["outcome"], r["failure_kind"]) == ("FAILED", "author")
    assert quote in r["feedback"] and r["feedback"] == out["error"] and r["bundle_sha256"]  # repair starts here
    assert out["cost_usd"] == pytest.approx(1.28) and r["leak"]["passed"] is False


def test_a_failed_review_call_does_not_fail_the_create(work):
    def broken(bundle_dir, trace_dir):
        raise RuntimeError("leak review returned unparseable JSON")

    out, _ = create(work, leak_review=broken)
    assert out["outcome"] == "OK" and "unparseable" in out["result"]["leak"]["error"] and out["cost_usd"] == 1.25


def test_a_leak_review_outside_the_bundle_store_stays_in_the_trace_dir(tmp_path):
    from validator import gates

    assert gates.leak_review_dir(tmp_path / "examples" / "x", tmp_path / "tr") == tmp_path / "tr" / "leak"
    assert gates.leak_review_dir(tmp_path / "bundles" / "abc", tmp_path / "tr") == tmp_path / "leak_reviews" / "abc"


# --- the world mutation prompt -------------------------------------------------------------


PLAN_OPERATORS = {"add_concurrent_execution", "inject_partial_failure", "introduce_global_invariant",
                  "generate_counterexample", "tighten_resource_bound", "add_persistent_state",
                  "extend_cross_component_dependency", "add_semantic_edge_case", "add_realistic_distractor",
                  "compose_failure_modes", "expand_state_space", "introduce_temporal_constraint"}


def test_every_plan_operator_and_remove_cues_is_explained():
    assert set(at.MUTATION_OPERATORS) == PLAN_OPERATORS | {"remove_cues"}


def test_world_mutation_instruction():
    this = {"version": 2, "operators": ["add_persistent_state"], "why": "the fix was process-local"}
    job = {"origin": "mutation", "parent_world_sha256": "w1", "spec": WORLD["spec"],
           "mutation": {"operators": ["add_persistent_state", "remove_cues"], "why": "the fix was process-local"},
           "mutations": [{"version": 1, "operators": ["generate_counterexample"]}, this],
           "analysis": {"why_easy": "one file, symptom next to cause", "simple_strategy": "an in-memory set",
                        "cues": [{"quote": "dedupe in memory", "where": "README"}]},
           "target_fix": "keep a process-local set of processed request ids"}
    text = at.render_world_mutation_instruction(job, WORLD)
    for needle in ("keep a process-local set", at.MUTATION_OPERATORS["add_persistent_state"],
                   at.MUTATION_OPERATORS["remove_cues"], "one file, symptom next to cause", '"dedupe in memory"',
                   "generate_counterexample", "/input/parent/", "/logs/artifacts/world/",
                   '"parent_world_sha256": "w1"', "sde-world check /logs/artifacts/world",
                   "confirmed reservations never exceed stock", "counterfactual_failure"):
        assert needle in text, needle
    assert "the fix was process-local" in text
    assert text.count('"operators": ["add_persistent_state"]') == 0  # this mutation isn't listed as history
    assert re.findall(r"\$\w+", text) == ["$SDE_DOCKER_LABEL"]


@pytest.mark.parametrize("operators", [[], ["make_it_harder"]])
def test_world_mutation_rejects_unknown_operators(operators):
    with pytest.raises(ValueError, match="mutation operators"):
        at.render_world_mutation_instruction({"mutation": {"operators": operators}}, WORLD)
