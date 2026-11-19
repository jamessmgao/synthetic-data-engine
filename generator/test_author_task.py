"""author_task: run_create_job against a fake authoring harness (no Harbor, no model)."""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from generator import author_task as at
from generator.families import brief_for
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


def checked(draft_path, world_dir, artifacts_dir):
    """A self-check the author ran and passed (the real guard: tests at the end)."""
    return {"ok": True, "by": "author", "feedback": "", "result": {"check": {"ok": True}, "runs": {"oracle": True}}}


def create(work, harness=None, leak_review=clean_review, self_check=checked, **job_input):
    harness = harness or FakeHarness()
    job_input = {"origin": "initial", "world_sha256": "w1", "shape": "fix", "dial": 2, **job_input}
    return at.run_create_job("t-1-v1-create", job_input, work, run_trial=harness, leak_review=leak_review,
                             self_check=self_check), harness


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
    assert harness.calls[0]["timeout_sec"] == at.AUTHOR_TIMEOUT_SEC == 7200


def test_instruction_carries_the_world_the_shape_and_the_rules(work):
    _, harness = create(work, shape="implement", dial=3)
    text = harness.calls[0]["instruction"]
    for needle in ("confirmed reservations never exceed stock", "A reads stock=1", "2 confirmed, stock 1",
                   "check-then-insert inside one transaction", "/input/world/", "/logs/artifacts/draft.json",
                   "sde-task check /logs/artifacts/draft.json", "sde-task run /logs/artifacts/draft.json --as all",
                   "Shape: implement", "level 3 of 3", at.FORMS["many_rules"]["dial"][3],
                   "python:3.12-slim-bookworm",  # system.md, images filled in
                   "# plausible_wrong_fix: <world.json's text, exactly>", "setup errors",
                   "the counterexample test must be among nop's failures",
                   "sde-task schema", "**Aim to be done in ~10 minutes.**", "by absolute `/app/...` paths",
                   # Invariants as properties over seeded schedules; no hooks; exact values.
                   "A dedicated test per invariant", "at least 10 seeds", "after every step",
                   "exact expected values", "crash hooks or stage names", "never import internals",
                   # Invariants are numbered for the `# invariant: N` markers sde-task check enforces.
                   "1. confirmed reservations never exceed stock", "`# invariant: N`", "rng = random.Random(seed)",
                   "## How Harbor runs your task", "## Rules the pipeline checks",
                   # P1: no counterexample schedule, no hook position, no fix shape; plain heredocs.
                   "where a crash fires relative to the effects", "GOOD (a real Terminal-Bench task)",
                   "never nest heredocs", "Timing assertions need a 5x margin", "one file or test group per response", "both must exit 0 before you finish", "is REQUIRED wherever it is described", "may emit `result_ready`",
                   "a short request in a user's voice", "discovers the tools itself", "never the checks it applies or the criteria an answer meets", "today's output is the requirement", "Each test starts from clean state it creates", "Every blocking read of a child process has a timeout", "a free port it picks, else stop a leftover listener", "unless the instruction states the overlap", "Make the suite comprehensive", "# Before you finish", "you are not done until both commands exit 0", "Never finish with a failing check"):
        assert needle in text, needle
    # Round 2: checklist first, and system.md's long guidance sections left to sde-task check.
    assert text.index("# Checklist") < text.index("# The draft")
    assert "## What makes the task good" not in text and "Mistakes earlier drafts made" not in text
    assert len(text) / 4 < 3500  # ≈ tokens
    assert set(re.findall(r"\$\w+", text)) == {"$SDE_DOCKER_LABEL"}  # every template slot filled
    assert "never kill, stop or remove\ncontainers you didn't start" in text or \
        "never kill, stop or remove containers you didn't start" in text.replace("\n", " ")
    assert "{base_images}" not in text and "This is a revision" not in text
    # system.md's one-shot output contract does not apply: there is no response schema here.
    assert "Return exactly one JSON object that matches the provided schema" not in text
    assert "This world already has a task" not in text


def test_a_reused_world_shows_its_earlier_tasks_and_asks_for_a_different_one(work):
    earlier = ["Orders are sometimes charged twice after a restart.\nNo order may be charged twice.",
               "x" * 1500]  # the worker's cap per instruction
    _, harness = create(work, reused=True, earlier_instructions=earlier)
    text = harness.calls[0]["instruction"]
    for needle in ("# This world already has a task", "Write a different one",
                   "another entry point or user-visible symptom", "never the same request reworded",
                   "Task 1:\n> Orders are sometimes charged twice after a restart.\n> No order may be charged twice.",
                   "Task 2:\n> xxx"):
        assert needle in text, needle
    assert text.index("# This world already has a task") < text.index("# Before you finish")
    assert len(text) / 4 < 4500  # ≈ tokens: the prompt plus up to 3 capped instructions
    # A revision keeps its own request; without earlier tasks there's no section.
    revision = at.render_task_author_instruction(WORLD, {"origin": "repair", "feedback": "nop passed",
                                                         "earlier_instructions": earlier}, "fix", 2)
    assert "This is a revision" in revision and "This world already has a task" not in revision
    assert at.earlier_tasks_section({"reused": True, "earlier_instructions": []}) == ""


def test_the_leak_review_flags_counterexample_shaped_text_and_crash_hooks():
    from validator import gates

    for needle in ("describes the failing scenario itself", "where a crash or fault hook fires",
                   "tick --milliseconds 30001", "must reuse that exact durable result",
                   "A crash hook or stage list in the instruction", "SEMANTICALLY", "Synonyms and paraphrases count",
                   "without reading\nand reasoning about the code", '"direct"', '"strong_hint"'):
        assert needle in gates.LEAK_SYSTEM, needle


def test_the_world_instruction_has_the_scaffolds_and_the_time_box():
    from generator import author

    text = author.render_world_instruction(WORLD["spec"])
    for needle in ("/opt/sde/scaffold/", "sde_sched.py", "sde_fault.py", "sde_repro.py",
                   # Round 2: a lighter v1 (difficulty comes from the mutation round).
                   "Aim to be done in ~12 minutes; run the self-check by ~10", "200-500 lines in 2-3 modules", "one module per step (≤ ~150 lines per response)", "never rewrite a working world to hit it", "delete the old file", "must exit 0 before you finish", "fix the single failing check", "5x margin (1 CPU)", "textbook slip agents spot on sight", "only on a deeper schedule", "`strtok_r` needs `_POSIX_C_SOURCE`", "every `COPY`",
                   "use them only if they save you time", "don't read sources you won't use",
                   "Go or C\nworlds: the README's last section",
                   # Kept: an executed counterexample, outcomes, no hooks, a tempting wrong fix.
                   "find the counterexample by EXECUTING it", "**Invariants are outcomes**",
                   "**No documented fault hooks or stage names**", "the fix is not a one-liner",
                   "Prefer a fix that touches 2 modules", "makes the visible\n  symptom go away but still fails"):
        assert needle in text, needle
    assert text.index("# Checklist") < text.index("# The spec") < text.index("# Rules")
    assert len(text) / 4 < 2200  # ≈ tokens (P1's was 2.1k, round 1's 3.1k)
    from shared.taskformat import world_check

    # Every file the world_check gate requires is asked for; the hardness ones are optional.
    for rel in (*(p for p in world_check.REQUIRED if p != "environment/Dockerfile"),
                *world_check.HARDNESS_FILES, "repro/dependent_check.sh"):
        assert f"`{rel}`" in text, rel
    assert "Optional (recorded as hardness when present)" in text
    for name in ("sde_sched.py", "sde_fault.py", "sde_repro.py"):  # the files it names exist
        assert (Path(__file__).resolve().parent.parent / "authoring" / "scaffold" / name).is_file()


def test_every_family_brief_names_its_lever_counterexample_and_dependent():
    """Short: world.md states the scope, the fault rule and the scaffolds once."""
    from generator.families import FAMILY_BRIEFS, brief_for

    for family in FAMILY_BRIEFS:
        brief = brief_for(family)
        for line in ("Strongest lever: ", "The counterexample: ", "Why a local patch may fail here: "):
            assert line in brief, (family, line)
        assert "Scaffolds" not in brief and "300-800" not in brief


def test_the_performance_brief_states_outcomes_and_steers_off_ledgers():
    """Performance tasks leaked when the bound named the fix ("index it", "stream it") and the
    worlds crowded into ledgers (the domain cap): the brief asks for outcomes only, other domains."""
    from generator.families import brief_for

    brief = brief_for("performance_resources")
    assert "stated as an outcome only" in brief and "never the structure, the tie-break or the edge case" in brief
    assert "not a ledger or payments system" in brief
    assert "the plausible wrong fix is that fast version" in brief
    assert "no name or comment that describes the cost" in brief  # aws-w1-l3-t04's find_pending scans


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
    return at.run_create_job("t-1-v2-revise", job_input, work, run_trial=harness, leak_review=clean_review,
                             self_check=checked), harness


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

    def complete(messages, response_format, llm_dir=None, cache_key=None, **_):
        calls.append({"llm_dir": llm_dir, "user": messages[1]["content"], "key": cache_key})
        content = {"leak-blind": {"predicted_fix": "unclear", "predicted_failure": "unclear"},
                   "leak-judge": {"match": "none", "why": "-"}}.get(cache_key, {"verdict": verdict,
                                                                              "leaks": list(leaks)})
        return llm.Completion(content=json.dumps(content), finish_reason="stop",
                              model="z-ai/glm-5.1", prompt_tokens=1, completion_tokens=1, reasoning_tokens=0,
                              gateway_cost_usd=None, cost_usd=cost, duration_sec=1.0, transport_retries=0,
                              request_sha256="r1", cached=False)

    monkeypatch.setattr(llm, "complete", complete)
    return calls


def test_create_runs_the_leak_review_and_validate_adopts_it(work, monkeypatch):
    calls = fake_llm(monkeypatch)
    out, _ = create(work, leak_review=None)  # the real gate_leak
    # Three calls: the semantic review, the blind prediction, the judgement (0.03 each).
    assert out["outcome"] == "OK" and out["cost_usd"] == pytest.approx(1.25 + 3 * 0.03)
    assert out["result"]["leak"]["passed"] and out["result"]["leak"]["adopted"] is False
    assert [c["key"] for c in calls] == ["leak-review", "leak-blind", "leak-judge"]
    # The reviewer sees the world the task came from (its counterexample and fix); the
    # blind predictor sees the instruction only.
    assert out["result"]["leak"]["had_world"] and "A reads stock=1" in calls[0]["user"]
    assert calls[1]["user"].startswith("INSTRUCTION\n\n") and "A reads stock=1" not in calls[1]["user"]
    sha = out["result"]["bundle_sha256"]
    assert (work / "leak_reviews" / sha / "review.json").is_file()
    from validator import gates

    gate = gates.gate_leak(Path(out["result"]["bundle_path"]), work / "traces" / "t-1-v1-validate-r0")
    assert gate.passed and gate.detail["adopted"] is True and gate.detail["cost_usd"] == 0.0
    assert len(calls) == 3  # validate did not call the model again


def test_a_leak_found_at_create_is_the_authors_fault_with_feedback(work, monkeypatch):
    instruction = VALID["instruction"]
    quote = instruction.split(".")[0].strip()  # a real sentence of the instruction
    fake_llm(monkeypatch, verdict="leak", leaks=[{"quote": quote, "reveals": "the fix"}])
    out, _ = create(work, leak_review=None)
    r = out["result"]
    assert (out["outcome"], r["failure_kind"]) == ("FAILED", "author")
    assert quote in r["feedback"] and r["feedback"] == out["error"] and r["bundle_sha256"]  # repair starts here
    assert out["cost_usd"] == pytest.approx(1.25 + 3 * 0.03) and r["leak"]["passed"] is False


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
                   "the parent w1", '"lineage": {"mutations": [{"defeats":', "sde-world check /logs/artifacts/world",
                   "confirmed reservations never exceed stock", "counterfactual_failure",
                   "Aim to be done in ~12 minutes", "200-500 lines in 2-3 modules", "change one module per response", "/opt/sde/scaffold/",
                   "A mutation is where difficulty is added",
                   "nor describe the failing schedule", "describe its schedule",
                   "If the strategy that passed was a local fix", "size-adding changes",
                   "no documented crash hook or stage names",
                   'tag your builds `"sde-dev-${SDE_DOCKER_LABEL#sde.job=}"`',
                   "signals, not gates; never rework a passing world"):
        assert needle in text, needle
    assert "the fix was process-local" in text
    # The family brief (no new worlds are built, so a mutation is where it reaches a world).
    assert "# What this family's defects look like" in text and brief_for("database_consistency") in text
    no_spec = at.render_world_mutation_instruction({k: v for k, v in job.items() if k != "spec"}, WORLD)
    assert brief_for("database_consistency") in no_spec  # from the parent world's spec
    assert text.count('"operators": ["add_persistent_state"]') == 0  # this mutation isn't listed as history
    assert re.findall(r"\$\w+", text) == ["$SDE_DOCKER_LABEL"]


def test_the_mutation_is_told_what_the_parents_hardness_checks_found():
    job = {"origin": "mutation", "parent_world_sha256": "w1", "spec": WORLD["spec"],
           "mutation": {"operators": ["extend_cross_component_dependency"], "why": "local fix"},
           "mutations": [], "analysis": {}, "target_fix": "x",
           # world_check's own record shape (shared/taskformat/world_check.py _hardness)
           "hardness": {"symptom_ok": False, "spans_or_dependent": False, "wrong_fix_ok": False,
                        "failed": ["fix_not_local", "wrong_fix_passes_symptom", "symptom_visible"],
                        "warnings": [], "details": {"fix_files": ["/app/worker.py"]}}}
    text = at.render_world_mutation_instruction(job, WORLD)
    assert "The parent world's checks found the following; the mutation must fix these:" in text
    assert "its fix is local (world_fix/fix.sh changes /app/worker.py)" in text
    assert "its plausible wrong fix does not fix the visible symptom, so it is not tempting" in text
    assert "survives repro.sh" not in text and "did not show the symptom on the world" in text
    assert re.findall(r"\$\w+", text) == ["$SDE_DOCKER_LABEL"]
    clean = {**job, "hardness": {"failed": [], "warnings": [], "details": {}}}
    assert "passed every hardness check" in at.render_world_mutation_instruction(clean, WORLD)
    assert "(not recorded for the parent world)" in at.render_world_mutation_instruction(
        {k: v for k, v in job.items() if k != "hardness"}, WORLD)


@pytest.mark.parametrize("operators", [[], ["make_it_harder"]])
def test_world_mutation_rejects_unknown_operators(operators):
    with pytest.raises(ValueError, match="mutation operators"):
        at.render_world_mutation_instruction({"mutation": {"operators": operators}}, WORLD)


# --- the end-of-trial self-check guard (generator/self_check.py, the real one) -------------


TAGGED = "# plausible_wrong_fix: check-then-insert inside one transaction at READ COMMITTED\n" + WRONG_FIX


def _run(role, ok=True, failing=(), reward=None):
    expected = 1.0 if role == "oracle" else 0.0
    tests = [{"name": n, "status": "failed", "message": "assert x"} for n in failing] + [
        {"name": "test_other", "status": "passed", "message": ""}]
    return {"as": role, "ok": ok, "expected": expected, "reward": expected if reward is None else reward,
            "tests": tests, "setup_errors": [], "output_tail": f"{role} output line"}


class FakeRoles:
    """sde_task.run_roles stand-in for the guard: records calls, returns scripted runs."""

    def __init__(self, **bad):
        self.calls, self.bad = [], bad

    def __call__(self, bundle, roles, repeat_oracle=False):
        self.calls.append({"bundle": bundle, "roles": list(roles), "repeat_oracle": repeat_oracle})
        return [_run(r, ok=r not in self.bad, failing=self.bad.get(r, ())) for r in roles]


def _real_guard(draft_path, world_dir, artifacts_dir, roles=None):
    from generator.self_check import ensure_task_checked

    return ensure_task_checked(draft_path, world_dir, artifacts_dir, run_roles=roles or FakeRoles())


def _stamp(artifacts: Path) -> str:
    """What sde-task stamps its saved results with: the final draft's content hash."""
    from shared.taskformat.materialize import content_sha256
    from shared.taskformat.schema import TaskDraft

    return content_sha256(TaskDraft.model_validate_json((Path(artifacts) / "draft.json").read_text()))


class Saved(FakeHarness):
    """A trial whose author saved its sde-task results (check.json + run-<role>.json), stamped
    for the draft it wrote (`stamp`: override, e.g. None for an old trial or a stale one)."""

    def __init__(self, *a, check_ok=True, runs=None, stamp="current", **kw):
        super().__init__(*a, **kw)
        self.check_ok, self.runs, self.stamp = check_ok, runs, stamp

    def __call__(self, job_id, *a, **kw):
        out = super().__call__(job_id, *a, **kw)
        evidence = Path(out["artifacts_dir"]) / "sde_task"
        evidence.mkdir()
        stamp = _stamp(out["artifacts_dir"]) if self.stamp == "current" else self.stamp
        (evidence / "check.json").write_text(json.dumps({"ok": self.check_ok, "stamp": stamp, "errors": [] if
                                                         self.check_ok else ["tests: no `# invariant: 1` marker"]}))
        for run in self.runs or [_run(r) for r in ("oracle", "nop", "wrong")]:
            (evidence / f"run-{run['as']}.json").write_text(json.dumps({**run, "stamp": stamp}))
        return out


def test_a_draft_that_fails_sde_task_check_is_sent_back_with_its_bundle(work):
    """The fixture draft does not quote the world's plausible wrong fix: the guard runs
    `sde-task check` itself, fails the job as the author's, and keeps the bundle so
    after_generation routes it to a repair. The leak review is not paid for."""
    reviews = []
    out, _ = create(work, self_check=_real_guard, leak_review=lambda b, t: reviews.append(b) or FakeGate())
    assert out["outcome"] == "FAILED" and out["result"]["failure_kind"] == "author"
    assert "finished without running `sde-task check`" in out["result"]["feedback"]
    assert "plausible_wrong_fix" in out["result"]["feedback"]
    assert out["result"]["bundle_sha256"] and out["result"]["self_check"]["by"] == "pipeline"
    assert reviews == []


MARKED = draft_with()["test_outputs"] + "\n\n# invariant: 1\n# invariant: 2\n"  # the fixture world has 2


def test_without_saved_runs_the_guard_runs_every_role_once(work):
    roles = FakeRoles()
    out, _ = create(work, FakeHarness(draft=draft_with(wrong_fix_script=TAGGED, test_outputs=MARKED)),
                    self_check=lambda d, w, a: _real_guard(d, w, a, roles))
    assert out["outcome"] == "OK", out["error"]
    checked = out["result"]["self_check"]
    assert (checked["by"], checked["runs"]) == ("pipeline", {"oracle": True, "nop": True, "wrong": True})
    assert checked["stamp"] and all(r["stamp"] == checked["stamp"] for r in checked["roles"].values())
    assert len(roles.calls) == 1 and roles.calls[0]["roles"] == ["oracle", "nop", "wrong"]
    assert roles.calls[0]["repeat_oracle"] is True  # the flake check, as `--as all` does


def test_the_authors_saved_check_and_runs_are_used_not_repeated(work):
    roles = FakeRoles()
    out, _ = create(work, Saved(), self_check=lambda d, w, a: _real_guard(d, w, a, roles))
    assert out["outcome"] == "OK" and out["result"]["self_check"]["by"] == "author"
    assert out["result"]["self_check"]["runs"] == {"oracle": True, "nop": True, "wrong": True}
    assert roles.calls == []  # nothing re-run


def test_a_saved_failing_oracle_fails_the_create_with_its_tests(work):
    """local-agent-b2-t01: the author finished with its own oracle failing; it must go to a
    repair with that evidence, not on to validate."""
    reviews = []
    runs = [_run("oracle", ok=False, failing=("test_invariant_1_materialized_rendering", "test_regression_edge"),
                 reward=0.0), _run("nop"), _run("wrong")]
    out, _ = create(work, Saved(runs=runs), self_check=_real_guard,
                    leak_review=lambda b, t: reviews.append(b) or FakeGate())
    r = out["result"]
    assert (out["outcome"], r["failure_kind"]) == ("FAILED", "author") and r["bundle_sha256"]  # → repair<k+1>
    assert "finished with failing `sde-task run` roles" in r["feedback"]
    assert "oracle: reward 0.0 (expected 1.0)" in r["feedback"]
    assert "test_invariant_1_materialized_rendering, test_regression_edge" in r["feedback"]
    assert "oracle output line" in r["feedback"] and reviews == []  # no leak review paid for


@pytest.mark.parametrize("missing", ["nop", "wrong"])
def test_a_missing_role_means_the_guard_runs_them_and_a_failure_counts(work, missing):
    runs = [_run(r) for r in ("oracle", "nop", "wrong") if r != missing]
    roles = FakeRoles(**{missing: ("test_invariant_1_each_order_reserves_at_most_one",)})
    valid = draft_with(wrong_fix_script=TAGGED, test_outputs=MARKED)  # the guard re-checks it before running
    out, _ = create(work, Saved(draft=valid, runs=runs), self_check=lambda d, w, a: _real_guard(d, w, a, roles))
    # only the missing role runs here; the author's other stamped runs stand
    assert [c["roles"] for c in roles.calls] == [[missing]] and out["outcome"] == "FAILED"
    assert "run here, it fails" in out["result"]["feedback"]
    assert f"{missing}: reward 0.0" in out["result"]["feedback"]


@pytest.mark.parametrize("stamp", [None, "0" * 64], ids=["old-trial-no-stamp", "stale-draft-changed-after"])
def test_saved_runs_count_only_for_the_final_draft(work, stamp):
    """A saved check or run whose stamp isn't the final draft's content hash (an old trial
    without stamps, or an author who edited after running) is run again here."""
    roles = FakeRoles()
    valid = draft_with(wrong_fix_script=TAGGED, test_outputs=MARKED)
    out, _ = create(work, Saved(draft=valid, stamp=stamp), self_check=lambda d, w, a: _real_guard(d, w, a, roles))
    assert out["outcome"] == "OK" and out["result"]["self_check"]["by"] == "pipeline"
    assert [c["roles"] for c in roles.calls] == [["oracle", "nop", "wrong"]]
    assert roles.calls[0]["repeat_oracle"] is True


def test_exactly_stamped_saved_runs_are_used_and_recorded_for_validate(work):
    roles = FakeRoles()
    valid = draft_with(wrong_fix_script=TAGGED, test_outputs=MARKED)
    out, _ = create(work, Saved(draft=valid), self_check=lambda d, w, a: _real_guard(d, w, a, roles))
    checked = out["result"]["self_check"]
    assert out["outcome"] == "OK" and checked["by"] == "author" and roles.calls == []
    assert checked["stamp"] and {r["stamp"] for r in checked["roles"].values()} == {checked["stamp"]}
    assert checked["roles"]["oracle"]["reward"] == 1.0 and checked["roles"]["nop"]["reward"] == 0.0


def test_a_saved_failing_check_fails_the_create(work):
    out, _ = create(work, Saved(check_ok=False), self_check=_real_guard)
    assert out["outcome"] == "FAILED" and "failed `sde-task check` and finished anyway" in out["result"]["feedback"]
    assert "no `# invariant: 1` marker" in out["result"]["feedback"]


# --- time boxes --------------------------------------------------------------------------


def test_the_instruction_size_reaches_the_result(work):
    class Sized(FakeHarness):
        def __call__(self, job_id, instruction, *a, **kw):
            return {**super().__call__(job_id, instruction, *a, **kw),
                    "instruction_size": {"chars": len(instruction), "tokens_est": round(len(instruction) / 4)}}

    out, harness = create(work, Sized())
    size = out["result"]["authoring"]["instruction_size"]
    assert size["chars"] == len(harness.calls[0]["instruction"]) and 2000 < size["tokens_est"] < 8000


def test_the_task_author_stops_at_15_minutes(work):
    _, harness = create(work)
    assert harness.calls[0]["timeout_sec"] == 7200


@pytest.mark.parametrize("module,var,default", [("author_task", "SDE_CREATE_TIMEOUT_S", 7200.0),
                                                ("author", "SDE_WORLD_TIMEOUT_S", 7200.0)])
def test_authoring_time_boxes_are_env_overridable(module, var, default):
    root = Path(__file__).resolve().parent.parent
    code = (f"import sys; sys.path[:0] = [{str(root / 'generator')!r}, {str(root / 'validator')!r}]; "
            f"import {module}; print({module}.AUTHOR_TIMEOUT_SEC)")
    env = {k: v for k, v in os.environ.items() if k != var}

    def timeout(extra):
        out = subprocess.run([sys.executable, "-c", code], env={**env, **extra}, capture_output=True,
                             text=True, check=True).stdout
        return float(out.strip().splitlines()[-1])

    assert timeout({}) == default and timeout({var: "900"}) == 900.0


# --- the parent world's hardness, as the mutation author sees it ----------------------------


def test_the_hardness_section_reads_world_checks_record():
    # world_check's own shape (shared/taskformat/world_check.py _hardness), from aws-iter1-t06's world.
    hardness = {"symptom_ok": True, "spans_or_dependent": False, "wrong_fix_ok": False,
                "failed": ["fix_not_local", "wrong_fix_fails_repro"], "warnings": ["limit_named"],
                "details": {"fix_files": ["recovery.py"], "limit_named": [
                    {"file": "/app/recovery.py", "function": "get_replay_target", "where": "comment",
                     "matched": ["committed", "only"], "text": "Only entries that have been committed"}]}}
    text = at.hardness_section(hardness)
    assert "changes recovery.py" in text and "the new one must fail the deep schedule" in text
    assert "`get_replay_target` names its own limit" in text and "passed every hardness check" not in text
    assert at.hardness_section({"failed": [], "warnings": [], "details": {}}).startswith("The parent world passed")
    assert at.hardness_section(None) == "(not recorded for the parent world)"


# --- prompt size over every spec the sampler can produce ---------------------------------
# The single-spec caps above (3500 / 2200 tokens) measure one fixture each. These measure the
# worst case over what production renders: every family x mechanism x shape (x dial), each
# with the domain whose label and note are longest. The caps sit a few percent above the
# worst case measured on 2026-11-18 (task author 3536, world 2688 tokens), so they catch
# growth; trimming below them is a separate decision.
TASK_AUTHOR_CAP_TOKENS = 3650
WORLD_CAP_TOKENS = 2800


def _taxonomy():
    root = str(Path(__file__).resolve().parent.parent)
    if root not in sys.path:
        sys.path.insert(0, root)
    from shared.taxonomy import taxonomy
    return taxonomy


def test_the_task_author_prompt_stays_under_its_cap_for_every_shape_and_dial():
    sizes = {(shape, dial): len(at.render_task_author_instruction(WORLD, {"origin": "initial"}, shape, dial)) / 4
             for shape in at.SHAPES for dial in (1, 2, 3)}
    worst = max(sizes, key=sizes.get)
    assert sizes[worst] < TASK_AUTHOR_CAP_TOKENS, (worst, sizes[worst])


def test_the_world_prompt_stays_under_its_cap_for_every_family_mechanism_and_shape():
    import random

    from generator import author
    tx = _taxonomy()
    worst = (0.0, None)
    for family, fam in tx.FAMILIES.items():
        domains = tx.COMPILER_DOMAINS if family == "compilers_parsers" else tx.APP_DOMAINS
        longest = max(domains, key=lambda d: len(domains[d]))
        for mechanism in fam.mechanisms:
            spec = tx.sample_spec_v2([], area=family, mechanism=mechanism, rng=random.Random(0), domain=longest)
            for shape in tx.SHAPES:
                n = len(author.render_world_instruction(dict(spec, shape=shape))) / 4
                worst = max(worst, (n, (family, mechanism, shape)), key=lambda w: w[0])
    assert worst[0] < WORLD_CAP_TOKENS, worst
