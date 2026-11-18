"""author.run_world_job against a fake authoring trial: origins, lineage, storage, failures."""

import json
import re
from pathlib import Path

import pytest

from generator import author

WORLD_JSON = {"name": "inventory-oversell", "invariants": ["confirmed reservations never exceed stock"],
              "counterexample": {"sequence": ["A reads stock=1", "B reads stock=1", "both confirm"],
                                 "reproduce": "repro/repro.sh", "observed": "2 confirmed, stock 1"},
              "plausible_wrong_fix": "check-then-insert inside one transaction at READ COMMITTED"}
SPEC = {"family": "database_consistency", "mechanism": "isolation_anomaly", "shape": "fix", "dial": 2}


def write_world(root: Path, **meta) -> Path:
    files = {"environment/Dockerfile": "FROM python:3.12-slim-bookworm\nWORKDIR /app\nCOPY app.py /app/\n",
             "environment/app.py": "print('reserve')\n", "repro/repro.sh": "exit 1\n",
             "repro/symptom_check.sh": "exit 1\n", "world_fix/fix.sh": "true\n",
             "world_fix/wrong_fix.sh": "true\n", "world.json": json.dumps({**WORLD_JSON, **meta})}
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    return root


class FakeTrial:
    """Stands in for authoring_harness.run_authoring_trial."""

    def __init__(self, tmp_path, world=True, artifacts=True, exception=None, self_check="OK", **meta):
        self.tmp, self.world, self.artifacts, self.exception, self.meta = tmp_path, world, artifacts, exception, meta
        self.self_check = self_check
        self.calls = []

    def __call__(self, job_id, instruction, files, work_dir, timeout_sec=3600):
        self.calls.append({"job_id": job_id, "instruction": instruction, "files": files, "timeout": timeout_sec})
        if not self.artifacts:
            return {"artifacts_dir": None, "error": "NoTrialResult: Harbor wrote no trial result",
                    "exception_type": "NoTrialResult", "cost_usd": 0.0}
        arts = self.tmp / "trial" / job_id / "artifacts"
        arts.mkdir(parents=True, exist_ok=True)
        if self.world:
            write_world(arts / "world", **self.meta)
            if self.self_check:
                from shared.taskformat.world_check import world_stamp

                (arts / "world_check.json").write_text(json.dumps({"outcome": self.self_check, "feedback": "x",
                                                                   "stamp": world_stamp(arts / "world")}))
        return {"artifacts_dir": str(arts), "trial_dir": str(arts.parent), "cost_usd": 1.25, "steps": 40,
                "duration_sec": 1800, "n_input_tokens": 900_000, "n_output_tokens": 20_000,
                "gateway_cost_usd": 5e-05, "exception_type": self.exception,
                "error": f"{self.exception}: x" if self.exception else None, "adopted": False}


def test_initial_world_is_stored_content_addressed_with_spec_and_empty_lineage(tmp_path):
    trial = FakeTrial(tmp_path)
    out = author.run_world_job("t1-v1-world", {"origin": "initial", "spec": SPEC}, tmp_path / "work", run_trial=trial)
    assert out["outcome"] == "OK" and out["cost_usd"] == 1.25
    sha = out["result"]["world_sha256"]
    stored = tmp_path / "work" / "worlds" / sha
    assert out["result"]["world_path"] == str(stored) and author.tree_sha256(stored) == sha
    meta = json.loads((stored / "world.json").read_text())
    assert meta["spec"] == SPEC and meta["lineage"] == {"parent_world_sha256": None, "mutations": []}
    assert out["result"]["name"] == "inventory-oversell"
    checked = out["result"]["authoring"]["self_check"]
    assert (checked["by"], checked["outcome"]) == ("author", "OK")
    # the stamp survives the world.json rewrite (spec, lineage): the world_check job can adopt it
    from shared.taskformat import world_check

    assert checked["stamp"] == checked["result"]["stamp"] == world_check.world_stamp(stored)
    ins = trial.calls[0]["instruction"]
    assert "Database consistency and recovery" in ins and '"mechanism": "isolation_anomaly"' in ins
    assert "$SDE_DOCKER_LABEL" in ins and "sde-world check /logs/artifacts/world" in ins
    assert "python:3.12-slim-bookworm" in ins and trial.calls[0]["files"] == {}


def test_storing_the_same_world_twice_gives_the_same_sha(tmp_path):
    a = author.run_world_job("a", {"spec": SPEC}, tmp_path / "work", run_trial=FakeTrial(tmp_path / "1"))
    b = author.run_world_job("b", {"spec": SPEC}, tmp_path / "work", run_trial=FakeTrial(tmp_path / "2"))
    assert a["result"]["world_sha256"] == b["result"]["world_sha256"]


def test_repair_gets_the_world_and_feedback_and_keeps_its_lineage(tmp_path):
    work = tmp_path / "work"
    lineage = {"parent_world_sha256": "p0", "mutations": [{"operators": ["inject_partial_failure"]}]}
    old = write_world(tmp_path / "old", lineage=lineage)
    sha = author.tree_sha256(old)
    (work / "worlds").mkdir(parents=True)
    old.rename(work / "worlds" / sha)
    trial = FakeTrial(tmp_path)
    out = author.run_world_job("t1-v1-world-repair1", {"origin": "repair", "world_sha256": sha,
                                                       "feedback": "repro.sh exited [0, 0]", "spec": SPEC},
                               work, run_trial=trial)
    assert out["outcome"] == "OK"
    call = trial.calls[0]
    assert "This is a REPAIR" in call["instruction"] and "repro.sh exited [0, 0]" in call["instruction"]
    assert "world/world.json" in call["files"] and "world/environment/Dockerfile" in call["files"]
    meta = json.loads((Path(out["result"]["world_path"]) / "world.json").read_text())
    assert meta["lineage"] == lineage


def test_mutation_sets_lineage_from_the_input(tmp_path):
    work = tmp_path / "work"
    parent = write_world(tmp_path / "p")
    psha = author.tree_sha256(parent)
    (work / "worlds").mkdir(parents=True)
    parent.rename(work / "worlds" / psha)
    mutations = [{"operators": ["add_persistent_state"], "why": "in-memory dedup passed"}]
    job = {"origin": "mutation", "parent_world_sha256": psha, "spec": SPEC,
           "mutation": {"operators": ["add_persistent_state"], "why": "in-memory dedup passed"},
           "mutations": mutations, "analysis": {"why_easy": "dedup in a dict"}, "target_fix": "a dict"}
    trial = FakeTrial(tmp_path)
    out = author.run_world_job("t1-v2-world", job, work, run_trial=trial)
    assert out["outcome"] == "OK"
    assert "parent/world.json" in trial.calls[0]["files"] and "/input/parent" in trial.calls[0]["instruction"]
    meta = json.loads((Path(out["result"]["world_path"]) / "world.json").read_text())
    assert meta["lineage"] == {"parent_world_sha256": psha, "mutations": mutations}


def test_a_variant_replaces_the_defect_with_a_reseeded_one_and_records_variant_of(tmp_path):
    work = tmp_path / "work"
    parent = write_world(tmp_path / "p")
    psha = author.tree_sha256(parent)
    (work / "worlds").mkdir(parents=True)
    parent.rename(work / "worlds" / psha)
    job = {"origin": "variant", "parent_world_sha256": psha, "variant_of": psha,
           "spec": {**SPEC, "mechanism": "write_skew"},
           "mutation": {"operators": ["generate_counterexample", "compose_failure_modes"],
                        "why": "a variant of a reused world"},
           "mutations": [], "earlier_instructions": ["Reservations sometimes oversell.\nFix it."]}
    trial = FakeTrial(tmp_path)
    out = author.run_world_job("t9-v1-world", job, work, run_trial=trial)
    assert out["outcome"] == "OK"
    text = trial.calls[0]["instruction"]
    # The variant opening and section replace the mutation's "too easy" framing.
    assert "too easy" not in text and "# Why it was too easy" not in text
    assert "mechanism **write_skew**" in text and "/input/parent/world_fix/fix.sh" in text
    assert "Reservations sometimes oversell. Fix it." in text
    assert "**generate_counterexample**" in text and "# The mutation to make" in text
    # Every slot is filled (only the shell's own variable is left), and where the shared sections
    # speak of a passing strategy, the variant says it means the parent's fix.
    assert re.findall(r"\$\w+", text) == ["$SDE_DOCKER_LABEL"], re.findall(r"\$\w+", text)
    assert "Remove the existing defect" in text and "read: the parent's fix" in text
    assert "**Mechanism: write_skew.**" in text  # the reseeded mechanism's own line
    assert "parent/world.json" in trial.calls[0]["files"]
    meta = json.loads((Path(out["result"]["world_path"]) / "world.json").read_text())
    assert meta["lineage"] == {"parent_world_sha256": None, "variant_of": psha, "mutations": []}
    assert meta["spec"]["mechanism"] == "write_skew"


def test_a_mutation_gets_the_passing_runs_traces_in_input_solver_runs(tmp_path):
    work = tmp_path / "work"
    parent = write_world(tmp_path / "p")
    psha = author.tree_sha256(parent)
    (work / "worlds").mkdir(parents=True)
    parent.rename(work / "worlds" / psha)
    job = {"origin": "mutation", "parent_world_sha256": psha, "spec": SPEC,
           "mutation": {"operators": ["extend_cross_component_dependency"], "why": ["one file"]},
           "mutations": [{}], "analysis": {"why_easy": "4 small files"}, "target_fix": "fix opt.go",
           "solver_runs": {"run1.md": "# run 1: passed in 15 agent steps", "../escape.md": "x"}}
    trial = FakeTrial(tmp_path)
    assert author.run_world_job("t1-v2-world", job, work, run_trial=trial)["outcome"] == "OK"
    call = trial.calls[0]
    assert call["files"]["solver_runs/run1.md"] == b"# run 1: passed in 15 agent steps"
    assert not any("escape" in f for f in call["files"])  # only run<k>.md names are staged
    assert "How the passing agents found it, step by step: /input/solver_runs/" in call["instruction"]
    del job["solver_runs"]
    trial = FakeTrial(tmp_path)
    author.run_world_job("t1-v2-world", job, work, run_trial=trial)
    assert "/input/solver_runs" not in trial.calls[0]["instruction"]


def test_a_bad_mutation_input_fails_without_running(tmp_path):
    work = tmp_path / "work"
    parent = write_world(tmp_path / "p")
    psha = author.tree_sha256(parent)
    (work / "worlds").mkdir(parents=True)
    parent.rename(work / "worlds" / psha)
    trial = FakeTrial(tmp_path)
    out = author.run_world_job("j", {"origin": "mutation", "parent_world_sha256": psha,
                                     "mutation": {"operators": ["make_it_hard"]}}, work, run_trial=trial)
    assert out["outcome"] == "FAILED" and "bad mutation input" in out["error"] and trial.calls == []


@pytest.mark.parametrize("trial_kw, kind", [
    ({"artifacts": False}, "infra"),                              # no trial at all
    ({"world": False, "exception": "AgentTimeoutError"}, "infra"),  # cut off before writing a world
    ({"world": False}, "author"),                                  # ran to the end, wrote nothing
    ({"invariants": []}, "author"),                                # a world, but not a valid one
])
def test_failures_say_whether_the_run_or_the_author_failed(tmp_path, trial_kw, kind):
    out = author.run_world_job("j", {"spec": SPEC}, tmp_path / "work", run_trial=FakeTrial(tmp_path, **trial_kw))
    assert out["outcome"] == "FAILED" and out["result"]["failure_kind"] == kind and out["result"]["world_sha256"] is None
    if kind == "author":
        assert out["result"]["feedback"]


def test_an_oversized_world_is_refused_with_feedback(tmp_path, monkeypatch):
    monkeypatch.setattr(author, "MAX_WORLD_FILES", 3)
    out = author.run_world_job("j", {"spec": SPEC}, tmp_path / "work", run_trial=FakeTrial(tmp_path))
    assert out["outcome"] == "FAILED" and "too large" in out["result"]["feedback"]


def test_a_world_whose_author_skipped_the_self_check_is_checked_once_here(tmp_path, monkeypatch):
    from shared.taskformat import world_check
    ran = []

    def check(world_dir, trace_dir):
        ran.append(Path(trace_dir).name)
        return {"outcome": "FAILED", "failed_check": "repro_fails", "feedback": "repro.sh exited [0, 0]"}

    monkeypatch.setattr(world_check, "run_world_check_job", check)
    out = author.run_world_job("j", {"spec": SPEC}, tmp_path / "work", run_trial=FakeTrial(tmp_path, self_check=None))
    assert ran == ["self_check"] and out["outcome"] == "FAILED" and out["result"]["failure_kind"] == "author"
    assert "without running `sde-world check`" in out["result"]["feedback"] and "[0, 0]" in out["result"]["feedback"]


def test_a_failing_self_check_fails_fast_without_rerunning(tmp_path, monkeypatch):
    from shared.taskformat import world_check
    monkeypatch.setattr(world_check, "run_world_check_job", lambda *a: pytest.fail("must not rerun"))
    out = author.run_world_job("j", {"spec": SPEC}, tmp_path / "work",
                               run_trial=FakeTrial(tmp_path, self_check="FAILED"))
    assert out["outcome"] == "FAILED" and "finished anyway" in out["result"]["feedback"]


def test_a_failed_world_check_names_the_check_for_the_retry(tmp_path, monkeypatch):
    """The retry/repair is told exactly which check failed: from the author's saved check,
    or from the one run here when the author skipped it."""
    from shared.taskformat import world_check

    class SavedFail(FakeTrial):
        def __call__(self, job_id, *a, **kw):
            out = super().__call__(job_id, *a, **kw)
            arts = Path(out["artifacts_dir"])
            (arts / "world_check.json").write_text(json.dumps(
                {"outcome": "FAILED", "failed_check": "build", "feedback": "COPY with more than one source file",
                 "stamp": world_check.world_stamp(arts / "world")}))
            return out

    saved = author.run_world_job("j1", {"spec": SPEC}, tmp_path / "w1", run_trial=SavedFail(tmp_path))
    assert saved["outcome"] == "FAILED" and saved["result"]["failure_kind"] == "author"
    assert saved["result"]["failed_check"] == "build"
    assert saved["result"]["feedback"].startswith("Failed check: build.") and "COPY" in saved["result"]["feedback"]

    monkeypatch.setattr(world_check, "run_world_check_job",
                        lambda w, t: {"outcome": "FAILED", "failed_check": "repro_fails", "feedback": "exited [0, 0]"})
    ours = author.run_world_job("j2", {"spec": SPEC}, tmp_path / "w2", run_trial=FakeTrial(tmp_path, self_check=None))
    assert ours["result"]["failed_check"] == "repro_fails" and ours["error"].startswith("Failed check: repro_fails.")

    passing = author.run_world_job("j3", {"spec": SPEC}, tmp_path / "w3", run_trial=FakeTrial(tmp_path))
    assert passing["outcome"] == "OK" and "failed_check" not in passing["result"]


def test_world_prompts_say_build_first_and_never_finish_on_a_failing_check():
    def text(name):  # the prompts wrap lines; compare on single spaces
        return " ".join((author.PROMPTS / name).read_text().split())

    world = text("world.md")
    assert "docker build -t sde-world-dev environment/" in world and "build again after every module" in world
    assert "`COPY a b c dest` needs `dest/`" in world
    for name in ("world.md", "world_mutation.md"):
        assert "Never finish with a failing or skipped check" in text(name), name


def test_a_mutation_keeps_what_the_author_says_it_defeats(tmp_path):
    work = tmp_path / "work"
    parent = write_world(tmp_path / "p")
    psha = author.tree_sha256(parent)
    (work / "worlds").mkdir(parents=True)
    parent.rename(work / "worlds" / psha)
    entry = {"operators": ["add_persistent_state"], "why": "in-memory dedup passed"}
    job = {"origin": "mutation", "parent_world_sha256": psha, "mutation": entry, "mutations": [entry]}
    trial = FakeTrial(tmp_path, lineage={"mutations": [{**entry, "defeats": "a dict keyed by request id"}]})
    out = author.run_world_job("t1-v2-world", job, work, run_trial=trial)
    meta = json.loads((Path(out["result"]["world_path"]) / "world.json").read_text())
    assert meta["lineage"]["mutations"] == [{**entry, "defeats": "a dict keyed by request id"}]


def test_run_create_job_is_re_exported():
    assert author.run_create_job.__module__ == "author_task"
