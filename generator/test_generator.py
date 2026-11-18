"""Tests for the deterministic half of the generator: schema checks and materialization.

No model calls: the drafts here are written by hand. Run with
    python -m pytest generator
"""

import copy
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from harbor.models.task.task import Task
from pydantic import ValidationError

from shared.contracts import config
from shared.llm import llm
from shared.taskformat.materialize import bundle_sha256, materialize, write_bundle
from shared.taskformat.schema import BASE_IMAGES, TaskDraft
from generator.worker import RESPONSE_FORMAT, run_generate_job

sys.path.insert(0, os.path.join(config.ROOT_DIR, "validator"))
from validator.validate import validate_task  # noqa: E402
from validator import gates  # noqa: E402,F401  (validator/gates.py)

VALID = {
    "name": "fix-median-calculation",
    "description": "Fix an off-by-one median calculation in a small stats CLI.",
    "difficulty": "easy",
    "category": "software-engineering",
    "tags": ["python", "debugging"],
    "expert_time_estimate_min": 5.0,
    "junior_time_estimate_min": 20.0,
    "spec": {
        "title": "Fix the median",
        "summary": "A stats CLI computes medians wrongly for even-length input.",
        "scenario": "A small CLI in /app reads numbers and prints their median.",
        "root_cause": "stats.median picks the upper middle element instead of averaging the two middles.",
        "correctness_constraints": [
            {"id": "C1", "statement": "Odd-length input prints the middle value."},
            {"id": "C2", "statement": "Even-length input prints the mean of the two middle values."},
        ],
        "subgoals": ["Find the faulty function", "Correct it"],
        "failure_scenario": "Running the CLI on 1 2 3 4 prints 3 instead of 2.5.",
        "expected_agent_pitfalls": ["Forgetting to sort the input"],
        "anti_shortcut_measures": ["Tests use inputs not shown in the instruction"],
    },
    "instruction": "The CLI at `/app/stats_cli.py` prints the median of its arguments. "
    "It is wrong for even-length input. Fix it so `python3 /app/stats_cli.py 1 2 3 4` prints `2.5`.",
    "dockerfile": "FROM python:3.12-slim-bookworm\nWORKDIR /app\nCOPY stats.py stats_cli.py /app/\n",
    "agent_writable_paths": [],
    "environment_files": [
        {"path": "stats.py", "content": "def median(xs):\n    xs = sorted(xs)\n    return xs[len(xs) // 2]\n"},
        {"path": "stats_cli.py", "content": "import sys\nfrom stats import median\n"
         "print(median([float(a) for a in sys.argv[1:]]))\n"},
    ],
    "solution_script": "cat > /app/stats.py <<'EOF'\ndef median(xs):\n    xs = sorted(xs)\n    n = len(xs)\n"
    "    mid = n // 2\n    return xs[mid] if n % 2 else (xs[mid - 1] + xs[mid]) / 2\nEOF\n",
    "solution_files": [],
    "test_outputs": "import subprocess\n\n\ndef run(*args):\n"
    "    return subprocess.run(['python3', '/app/stats_cli.py', *args], capture_output=True, text=True, "
    "timeout=30).stdout.strip()\n\n\ndef test_odd():\n    assert run('3', '1', '2') == '2.0'\n\n\n"
    "def test_even():\n    assert run('4', '1', '3', '2') == '2.5'\n\n\n"
    "def test_even_unsorted():\n    assert run('10', '0') == '5.0'\n",
    "test_support_files": [],
    "coverage": [
        {"requirement_id": "C1", "tests": ["test_odd"]},
        {"requirement_id": "C2", "tests": ["test_even", "test_even_unsorted"]},
    ],
}


def draft_with(**changes) -> dict:
    d = copy.deepcopy(VALID)
    d.update(changes)
    return d


def errors_of(data: dict) -> str:
    with pytest.raises(ValidationError) as e:
        TaskDraft.model_validate(data)
    return str(e.value)


def tree_digest(root: Path) -> dict[str, str]:
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()}


def test_valid_draft_materializes_into_a_valid_harbor_task(tmp_path):
    task_dir = materialize(TaskDraft.model_validate(VALID), tmp_path / "t", {"run_id": "r1"})

    result = validate_task(str(task_dir))
    assert result["passed"], result["errors"]
    assert Task(task_dir).name == "synthetic-data-engine/fix-median-calculation"
    for rel in ["task.toml", "instruction.md", "README.md", "environment/Dockerfile", "environment/stats.py",
                "solution/solve.sh", "tests/test.sh", "tests/test_outputs.py"]:
        assert (task_dir / rel).is_file(), rel
    for script in ["solution/solve.sh", "tests/test.sh"]:
        assert os.access(task_dir / script, os.X_OK)
        assert (task_dir / script).read_text().startswith("#!/bin/bash\n")
        assert subprocess.run(["bash", "-n", str(task_dir / script)]).returncode == 0
    for rel in ["environment/Dockerfile", "solution/solve.sh", "tests/test_outputs.py", "tests/test.sh"]:
        assert config.CANARY_GUID in (task_dir / rel).read_text(), rel
    # A reward file planted by the agent (or by its code, run as root during the
    # tests) must never survive: cleared before and after pytest.
    test_sh = (task_dir / "tests" / "test.sh").read_text()
    pytest_at = test_sh.index("pytest --ctrf")
    clear = "rm -f /logs/verifier/reward.txt /logs/verifier/reward.json"
    assert test_sh.index(clear) < pytest_at < test_sh.rindex(clear) < test_sh.index("echo 1 >")
    assert config.CANARY_GUID not in (task_dir / "instruction.md").read_text()
    assert not list(tmp_path.glob(".*tmp*")), "temporary directory left behind"


def test_materialize_is_deterministic(tmp_path):
    draft = TaskDraft.model_validate(VALID)
    a = materialize(draft, tmp_path / "a", {"run_id": "r1"})
    b = materialize(draft, tmp_path / "b", {"run_id": "r1"})
    assert tree_digest(a) == tree_digest(b)


def test_materialize_ignores_metadata_key_order(tmp_path):
    draft = TaskDraft.model_validate(VALID)
    a = materialize(draft, tmp_path / "a", {"run_id": "r1", "attempts": 1})
    b = materialize(draft, tmp_path / "b", {"attempts": 1, "run_id": "r1"})
    assert tree_digest(a) == tree_digest(b)


def test_materialize_refuses_to_overwrite(tmp_path):
    draft = TaskDraft.model_validate(VALID)
    materialize(draft, tmp_path / "t")
    with pytest.raises(FileExistsError):
        materialize(draft, tmp_path / "t")


def test_task_toml_comes_from_pipeline_defaults(tmp_path):
    task = Task(materialize(TaskDraft.model_validate(VALID), tmp_path / "t", {"run_id": "r1"}))
    assert task.config.agent.timeout_sec == config.AGENT_TIMEOUT_SEC
    assert task.config.environment.docker_image is None
    assert task.config.metadata["generation"] == {"run_id": "r1"}


def test_response_format_is_strict_compatible():
    """Strict JSON-schema mode needs closed objects with every property required."""
    schema = RESPONSE_FORMAT["json_schema"]["schema"]
    objects = [schema, *schema.get("$defs", {}).values()]
    for obj in objects:
        assert obj.get("additionalProperties") is False, obj.get("title")
        assert set(obj["required"]) == set(obj["properties"]), obj.get("title")


@pytest.mark.parametrize("path", ["../escape.py", "/abs.py", "a/../../b.py", "a\\b.py", "./x.py"])
def test_rejects_unsafe_paths(path):
    files = VALID["environment_files"] + [{"path": path, "content": "x = 1\n"}]
    assert "path" in errors_of(draft_with(environment_files=files))


def test_rejects_copy_of_missing_file():
    msg = errors_of(draft_with(dockerfile="FROM python:3.12-slim-bookworm\nWORKDIR /app\nCOPY missing.py /app/\n"))
    assert "'missing.py' does not exist" in msg


def test_rejects_copying_tests_into_image():
    msg = errors_of(draft_with(dockerfile="FROM python:3.12-slim-bookworm\nWORKDIR /app\nCOPY tests /tests\n"))
    assert "never be copied" in msg


def test_rejects_unlisted_base_image_and_missing_workdir():
    msg = errors_of(draft_with(dockerfile="FROM python:3.12-alpine\nCOPY stats.py /app/\n"))
    assert "not an allowed base image" in msg and "WORKDIR" in msg


def test_rejects_model_written_digest():
    msg = errors_of(draft_with(dockerfile="FROM python:3.12-slim-bookworm@sha256:abc\nWORKDIR /app\n"))
    assert "without a digest" in msg


def test_accepts_multi_stage_build():
    TaskDraft.model_validate(draft_with(dockerfile=(
        "FROM golang:1.23-bookworm AS build\nWORKDIR /src\nCOPY stats.py .\n"
        "FROM python:3.12-slim-bookworm\nWORKDIR /app\nCOPY --from=build /src/stats.py /app/\n"
        "COPY stats_cli.py /app/\n")))


@pytest.mark.parametrize("path", ["/usr", "/usr/local/lib", "/etc", "relative/dir", "/var", "/tests/x",
                                  "/data;rm"])
def test_rejects_bad_writable_paths(path):
    assert "agent_writable_paths" in errors_of(draft_with(agent_writable_paths=[path]))


def test_accepts_service_owned_paths():
    TaskDraft.model_validate(draft_with(agent_writable_paths=["/var/lib/stats", "/etc/stats", "/srv/data"]))


def test_dockerfile_is_pinned_and_creates_the_agent_user(tmp_path):
    draft = TaskDraft.model_validate(draft_with(agent_writable_paths=["/var/lib/stats"]))
    task = Task(materialize(draft, tmp_path / "t"))
    dockerfile = (tmp_path / "t" / "environment" / "Dockerfile").read_text()
    assert f"FROM python:3.12-slim-bookworm@{BASE_IMAGES['python:3.12-slim-bookworm']}" in dockerfile
    assert "useradd --create-home --shell /bin/bash agent" in dockerfile
    assert "chown -R agent:agent /app /var/lib/stats" in dockerfile
    assert 'echo "export PATH=\\"$PATH\\"" > /etc/profile.d/00-image-path.sh' in dockerfile
    assert task.config.agent.user == "agent"
    assert task.config.verifier.user is None  # verifier stays root


def test_write_bundle_is_content_addressed_and_idempotent(tmp_path):
    draft = TaskDraft.model_validate(VALID)
    sha1, path1 = write_bundle(draft, tmp_path, {"model": "m"})
    sha2, path2 = write_bundle(draft, tmp_path, {"model": "m"})
    assert (sha1, path1) == (sha2, path2) and path1.name == sha1
    assert bundle_sha256({p: (path1 / p).read_text() for p in
                          [str(f.relative_to(path1)) for f in path1.rglob("*") if f.is_file()]}) == sha1
    sha3, _ = write_bundle(draft.model_copy(update={"instruction": VALID["instruction"] + " More."}), tmp_path)
    assert sha3 != sha1


def test_accepts_directory_copy_and_multiline_instructions():
    TaskDraft.model_validate(draft_with(
        dockerfile="FROM python:3.12-slim-bookworm\nWORKDIR /app\nRUN pip install \\\n    requests==2.32.3\nCOPY . /app/\n"))


def test_rejects_reserved_paths():
    files = VALID["environment_files"] + [{"path": "Dockerfile", "content": "FROM x\n"}]
    assert "must not contain ['Dockerfile']" in errors_of(draft_with(environment_files=files))


def test_rejects_python_syntax_error():
    files = [{"path": "stats.py", "content": "def median(xs)\n    return 1\n"}, VALID["environment_files"][1]]
    assert "environment/stats.py has a Python syntax error" in errors_of(draft_with(environment_files=files))


def test_rejects_shell_syntax_error():
    assert "bash syntax error" in errors_of(draft_with(solution_script="if then fi\n"))


def test_rejects_third_party_imports_in_tests():
    msg = errors_of(draft_with(test_outputs="import requests\n" + VALID["test_outputs"]))
    assert "imports ['requests']" in msg


def test_allows_helper_module_imports_in_tests():
    TaskDraft.model_validate(draft_with(
        test_outputs="import helpers\n" + VALID["test_outputs"],
        test_support_files=[{"path": "helpers.py", "content": "X = 1\n"}]))


def test_rejects_too_few_tests():
    msg = errors_of(draft_with(test_outputs="def test_one():\n    assert True\n",
                               coverage=[{"requirement_id": "C1", "tests": ["test_one"]},
                                         {"requirement_id": "C2", "tests": ["test_one"]}]))
    assert "fewer than 3" in msg


def test_rejects_uncovered_constraint_and_unknown_test():
    msg = errors_of(draft_with(coverage=[{"requirement_id": "C1", "tests": ["test_missing"]}]))
    assert "['C2'] have no tests" in msg and "test_missing" in msg


def test_rejects_instruction_that_mentions_tests():
    msg = errors_of(draft_with(instruction="Make /tests/test_outputs.py pass."))
    assert "grading internals" in msg


@pytest.mark.parametrize("comment", ["# BUG: off by one", "# the bug is here", "# FIXME wrong middle"])
def test_rejects_comments_that_point_at_the_defect(comment):
    files = [{"path": "stats.py", "content": f"def median(xs):\n    {comment}\n    return sorted(xs)[len(xs) // 2]\n"},
             VALID["environment_files"][1]]
    assert "points at the defect" in errors_of(draft_with(environment_files=files))


def test_allows_ordinary_comments_mentioning_debug():
    files = [{"path": "stats.py", "content": "import logging  # debug logging\ndef median(xs):\n"
              "    return sorted(xs)[len(xs) // 2]\n"}, VALID["environment_files"][1]]
    TaskDraft.model_validate(draft_with(environment_files=files))


@pytest.mark.parametrize("heading", ["## Key Files to Investigate", "## Approach", "**Hints:**", "Where to look:"])
def test_rejects_hint_sections_in_instruction(heading):
    msg = errors_of(draft_with(instruction=VALID["instruction"] + f"\n\n{heading}\n\n- stats.py"))
    assert "hint sections" in msg


def test_rejects_bad_slug():
    assert "name" in errors_of(draft_with(name="Fix Median!"))


# --- the generate job, with the model call replaced (no spend) ----------------

class FakeModel:
    """Stands in for llm._stream: returns queued responses and counts calls."""

    def __init__(self, *responses: str):
        self.responses = list(responses)
        self.calls = 0

    def __call__(self, messages, response_format):
        self.calls += 1
        self.last_messages = messages
        return llm.Completion(content=self.responses.pop(0), finish_reason="stop", model="fake",
                              prompt_tokens=100, completion_tokens=1000, reasoning_tokens=0,
                              gateway_cost_usd=None, cost_usd=0.01, duration_sec=0.1, transport_retries=0)


def test_generate_job_writes_bundle_and_records_calls(tmp_path, monkeypatch):
    monkeypatch.setattr(llm, "_stream", FakeModel(json.dumps(VALID)))
    res = run_generate_job("t01-v1-generate-r0", {"origin": "initial", "prompt": "make a task"}, tmp_path)
    assert res["outcome"] == "OK", res["error"]
    r = res["result"]
    assert Path(r["bundle_path"]).name == r["bundle_sha256"]
    assert validate_task(r["bundle_path"])["passed"]
    assert res["cost_usd"] == 0.01 and len(r["llm_calls"]) == 1
    assert len(list((tmp_path / "llm").glob("*.json"))) == 1
    assert (tmp_path / "drafts" / f"{r['bundle_sha256']}.json").exists()


def test_generate_job_rerun_reuses_cached_calls(tmp_path, monkeypatch):
    fake = FakeModel(json.dumps(VALID))
    monkeypatch.setattr(llm, "_stream", fake)
    first = run_generate_job("t01-v1-generate-r0", {"origin": "initial", "prompt": "p"}, tmp_path)
    again = run_generate_job("t01-v1-generate-r0", {"origin": "initial", "prompt": "p"}, tmp_path)
    assert fake.calls == 1, "a retried job must not pay for a finished call again"
    assert again["result"]["bundle_sha256"] == first["result"]["bundle_sha256"]
    assert again["cost_usd"] == 0 and again["result"]["llm_calls"][0]["cached"]


def test_different_jobs_with_the_same_prompt_are_not_cached_together(tmp_path, monkeypatch):
    fake = FakeModel(json.dumps(VALID), json.dumps(VALID))
    monkeypatch.setattr(llm, "_stream", fake)
    run_generate_job("t01-v1-generate-r0", {"origin": "initial", "prompt": "p"}, tmp_path)
    run_generate_job("t02-v1-generate-r0", {"origin": "initial", "prompt": "p"}, tmp_path)
    assert fake.calls == 2


def test_generate_job_feeds_schema_errors_back(tmp_path, monkeypatch):
    fake = FakeModel(json.dumps(draft_with(instruction="Make /tests/test_outputs.py pass.")), json.dumps(VALID))
    monkeypatch.setattr(llm, "_stream", fake)
    res = run_generate_job("t01-v1-generate-r0", {"origin": "initial", "prompt": "p"}, tmp_path)
    assert res["outcome"] == "OK" and len(res["result"]["llm_calls"]) == 2
    assert "grading internals" in fake.last_messages[-1]["content"]


def test_repair_continues_the_conversation_and_guards_tests(tmp_path, monkeypatch):
    monkeypatch.setattr(llm, "_stream", FakeModel(json.dumps(VALID)))
    first = run_generate_job("t01-v1-generate-r0", {"origin": "initial", "prompt": "the prompt"}, tmp_path)
    sha = first["result"]["bundle_sha256"]

    dropped = draft_with(
        test_outputs=VALID["test_outputs"].replace("def test_even_unsorted", "def check_even_unsorted")
        + "\n\ndef test_extra():\n    assert True\n",
        coverage=[{"requirement_id": "C1", "tests": ["test_odd"]}, {"requirement_id": "C2", "tests": ["test_even"]}])
    fixed = draft_with(instruction=VALID["instruction"] + " Print one value per run.")
    fake = FakeModel(json.dumps(dropped), json.dumps(fixed))
    monkeypatch.setattr(llm, "_stream", fake)
    res = run_generate_job("t01-v1-repair1", {"origin": "repair", "bundle_sha256": sha,
                                              "feedback": "FAILED test_even: expected 2.5"}, tmp_path)
    assert res["outcome"] == "OK", res["error"]
    assert "removed test functions ['test_even_unsorted']" in res["result"]["llm_calls"][0]["errors"]
    sent = fake.last_messages
    assert sent[1] == {"role": "user", "content": "the prompt"} and sent[2]["role"] == "assistant"
    assert "expected 2.5" in sent[3]["content"]
    assert res["result"]["bundle_sha256"] != sha


def test_upstream_mid_stream_errors_are_retried_but_4xx_are_not():
    import httpx
    import openai
    req = httpx.Request("POST", "https://x/v1/chat/completions")
    upstream = openai.APIError("Upstream error from Alibaba: Model output became abnormal", req, body=None)
    bad_key = openai.AuthenticationError("Unauthorized", response=httpx.Response(401, request=req), body=None)
    assert llm._retryable(upstream)
    assert not llm._retryable(bad_key)


def test_generate_job_reports_bad_input(tmp_path):
    res = run_generate_job("x", {"origin": "repair", "bundle_sha256": "nope", "feedback": ""}, tmp_path)
    assert res["outcome"] == "FAILED" and "bad job input" in res["error"]


# --- the authoring prompt: form and dial, no restated rules --------------------

def test_prompt_uses_default_form_and_dial_and_drops_fixed_sections():
    from generator import prompting
    text = prompting.render_initial_prompt({"taxonomy": {"domain": "Databases"},
                                            "difficulty": {"level": "hard", "relevant_files": "15-30"}})
    assert "TASK FORM: Coupled behavior-preserving change" in text
    assert "DIFFICULTY DIAL: hidden dependents of the changed behavior, level 2 of 3" in text
    assert "Domain: Databases" in text
    assert "relevant repository files" not in text, "generic counts are no longer rendered"
    assert "INSTRUCTION QUALITY" not in text and "TEST REQUIREMENTS" not in text
    assert text.rstrip().endswith(prompting.CLOSING)


def test_prompt_seed_still_gets_a_form_and_dial():
    from generator import prompting
    text = prompting.render_initial_prompt({"seed": "a cache that serves stale prices",
                                            "difficulty": {"form": "many_rules", "dial": 3}})
    assert "TASK IDEA (from the requester)" in text and "a cache that serves stale prices" in text
    assert "TASK FORM: Implementation against many precise rules" in text
    assert "level 3 of 3" in text and prompting.FORMS["many_rules"]["dial"][3] in text


def test_prompt_fixed_sections_only_for_the_ab():
    from generator import prompting
    with_fixed = prompting.render_initial_prompt({"difficulty": {"fixed_sections": True}})
    assert "INSTRUCTION QUALITY" in with_fixed and with_fixed.rstrip().endswith(prompting.CLOSING)


def test_prompt_rejects_unknown_form_and_clamps_dial():
    from generator import prompting
    with pytest.raises(ValueError):
        prompting.render_initial_prompt({"difficulty": {"form": "nonsense"}})
    assert "level 3 of 3" in prompting.render_initial_prompt({"difficulty": {"dial": 9}})


ANALYSIS = {"why_easy": "All runs patched drain() in 6 steps.", "simple_strategy": "patch drain() locally",
            "cues": [{"quote": "jobs are acknowledged early", "where": "instruction"}],
            "fix_applied": "moved the ack after the write", "trusted": True,
            "suggestions": [{"technique": "preserve_guarantees", "change": "make stats depend on the old ordering",
                             "defeats": "the local reorder breaks the stats"}]}


def test_harder_revision_states_the_gap_the_analysis_and_the_techniques():
    from generator import prompting
    text = prompting.render_revision_request({"origin": "revision", "operator": "increase_diagnosis_depth",
                                              "result": "5 of 5 runs passed.", "evidence": "- run 1: passed",
                                              "direction": "harder", "analysis": ANALYSIS})
    assert "5 of 5 runs passed. That is too easy. The target is a task that 1 to 3 of 5 runs pass." in text
    for must in ("Why the runs passed", "patch drain() locally", "\"jobs are acknowledged early\" (instruction)",
                 "[preserve_guarantees] make stats depend on the old ordering", "How to make it harder",
                 "counterfactual_failure", "Do NOT make it harder through vague", "- run 1: passed",
                 prompting.OPERATORS["increase_diagnosis_depth"]):
        assert must in text, must


def test_untrusted_analysis_is_left_out_and_direction_is_inferred():
    from generator import prompting
    text = prompting.render_revision_request({"operator": "increase_diagnosis_depth", "result": "4 of 5 runs passed.",
                                              "analysis": {**ANALYSIS, "trusted": False}})
    assert "too easy" in text and "Why the runs passed" not in text


def test_easier_and_repair_revisions_get_their_own_guidance():
    from generator import prompting
    easier = prompting.render_revision_request({"operator": "localize_diagnosis", "result": "0 of 5 runs passed."})
    assert "That is too hard." in easier and "How to make it easier" in easier
    assert "How to make it harder" not in easier
    repair = prompting.render_revision_request({"operator": "repair_ambiguity", "result": "2 of 5 runs passed."})
    assert "the task itself is defective" in repair and "without changing the difficulty" in repair


def test_revision_job_sends_the_new_request(tmp_path, monkeypatch):
    monkeypatch.setattr(llm, "_stream", FakeModel(json.dumps(VALID)))
    first = run_generate_job("t01-v1-create", {"origin": "initial", "prompt": "the prompt"}, tmp_path)
    fake = FakeModel(json.dumps(draft_with(instruction=VALID["instruction"] + " Also report the mean.")))
    monkeypatch.setattr(llm, "_stream", fake)
    res = run_generate_job("t01-v2-revise", {"origin": "revision", "bundle_sha256": first["result"]["bundle_sha256"],
                                             "operator": "increase_constraint_interaction", "direction": "harder",
                                             "result": "5 of 5 runs passed.", "evidence": "- run 1: passed",
                                             "analysis": ANALYSIS}, tmp_path)
    assert res["outcome"] == "OK", res["error"]
    assert "patch drain() locally" in fake.last_messages[-1]["content"]
    meta = json.loads((tmp_path / "drafts" / f"{res['result']['bundle_sha256']}.json").read_text())["generation_meta"]
    assert meta["direction"] == "harder" and meta["operator"] == "increase_constraint_interaction"


# --- the wrong-fix gate (TB1) ---------------------------------------------------

WRONG_FIX = "cat > /app/stats.py <<'EOF'\ndef median(xs):\n    xs = sorted(xs)\n    return xs[len(xs) // 2 - 1]\nEOF\n"


def test_old_drafts_without_a_wrong_fix_still_load_and_materialize_without_one(tmp_path):
    draft = TaskDraft.model_validate(VALID)  # VALID predates wrong_fix_script
    assert draft.wrong_fix_script == ""
    task_dir = materialize(draft, tmp_path / "t")
    assert not (task_dir / "solution" / "wrong_fix.sh").exists()


def test_wrong_fix_is_materialized_into_solution_only(tmp_path):
    task_dir = materialize(TaskDraft.model_validate(draft_with(wrong_fix_script=WRONG_FIX)), tmp_path / "t")
    wrong = task_dir / "solution" / "wrong_fix.sh"
    assert wrong.read_text().startswith("#!/bin/bash\n") and os.access(wrong, os.X_OK)
    assert "len(xs) // 2 - 1" in wrong.read_text()
    assert "wrong_fix" not in (task_dir / "environment" / "Dockerfile").read_text()


def test_wrong_fix_must_differ_from_the_solution_and_parse():
    assert "identical to solution_script" in errors_of(draft_with(wrong_fix_script=VALID["solution_script"]))
    assert "wrong_fix.sh has a bash syntax error" in errors_of(draft_with(wrong_fix_script="if then fi\n"))
    files = [{"path": "wrong_fix.sh", "content": "echo x\n"}]
    assert "must not contain ['wrong_fix.sh']" in errors_of(draft_with(solution_files=files))


def test_response_schema_requires_the_wrong_fix():
    schema = RESPONSE_FORMAT["json_schema"]["schema"]
    assert "wrong_fix_script" in schema["required"]


def _outcome(reward, exc=None):
    return gates.TrialOutcome(trial_dir=None, reward=reward, exception_type=exc, exception_message=None,
                              tests=[], adopted=False)


def test_wrong_fix_gate_skips_bundles_without_one(tmp_path):
    bundle = materialize(TaskDraft.model_validate(VALID), tmp_path / "b")
    gate = gates.gate_wrong_fix(bundle, tmp_path / "trace")
    assert gate.passed and "no solution/wrong_fix.sh" in gate.detail["skipped"]


def test_wrong_fix_gate_runs_the_wrong_fix_as_the_oracle(tmp_path, monkeypatch):
    bundle = materialize(TaskDraft.model_validate(draft_with(wrong_fix_script=WRONG_FIX)), tmp_path / "b")
    seen = {}

    def fake_step(bundle_dir, agent, trace_dir, step):
        seen.update(agent=agent, step=step, solve=(bundle_dir / "solution" / "solve.sh").read_text(),
                    has_wrong=(bundle_dir / "solution" / "wrong_fix.sh").exists())
        return _outcome(0.0)

    monkeypatch.setattr(gates, "run_harbor_step", fake_step)
    gate = gates.gate_wrong_fix(bundle, tmp_path / "trace")
    assert gate.passed
    assert seen["agent"] == "oracle" and seen["step"] == "wrong_fix" and not seen["has_wrong"]
    assert "len(xs) // 2 - 1" in seen["solve"]
    assert (bundle / "solution" / "solve.sh").read_text() != seen["solve"], "the real bundle is untouched"


def test_wrong_fix_gate_fails_when_the_tests_accept_it(tmp_path, monkeypatch):
    bundle = materialize(TaskDraft.model_validate(draft_with(wrong_fix_script=WRONG_FIX)), tmp_path / "b")
    monkeypatch.setattr(gates, "run_harbor_step", lambda *a: _outcome(1.0))
    gate = gates.gate_wrong_fix(bundle, tmp_path / "trace")
    assert not gate.passed and "ACCEPT the plausible wrong fix" in gate.feedback and "len(xs)" in gate.feedback


def test_wrong_fix_gate_fails_without_a_clean_verdict(tmp_path, monkeypatch):
    bundle = materialize(TaskDraft.model_validate(draft_with(wrong_fix_script=WRONG_FIX)), tmp_path / "b")
    monkeypatch.setattr(gates, "run_harbor_step", lambda *a: _outcome(None, "RewardFileNotFoundError"))
    gate = gates.gate_wrong_fix(bundle, tmp_path / "trace")
    assert not gate.passed and "did not produce a clean verdict" in gate.feedback


def test_validate_job_runs_wrong_fix_after_nop_and_before_leak(tmp_path, monkeypatch):
    order = []
    ok = lambda name: (order.append(name), gates.Gate(name, True, {}))[1]  # noqa: E731
    monkeypatch.setattr(gates, "gate_structural", lambda b: ok("structural"))
    monkeypatch.setattr(gates, "gate_lint", lambda b: ok("lint"))
    monkeypatch.setattr(gates, "gate_oracle", lambda b, t, step: ok(step))
    monkeypatch.setattr(gates, "gate_nop", lambda b, t: ok("nop"))
    monkeypatch.setattr(gates, "gate_wrong_fix", lambda b, t: ok("wrong_fix"))
    monkeypatch.setattr(gates, "gate_leak", lambda b, t: ok("leak"))
    res = gates.run_validate_job(tmp_path, tmp_path / "trace")
    assert res["outcome"] == "OK" and order == ["structural", "lint", "oracle1", "oracle2", "nop", "wrong_fix", "leak"]


# --- the world_check gate (TB1) --------------------------------------------------

def make_world(root: Path, **over) -> Path:
    """A tiny world: a dedup helper that keeps duplicates; repro exits 1 while it does."""
    w = root / "w0rld"
    files = {
        "environment/Dockerfile": "FROM python:3.12-slim-bookworm\nWORKDIR /app\nCOPY dedup.py /app/\n",
        "environment/dedup.py": "def dedup(xs):\n    return list(xs)\n",
        "repro/repro.sh": "cd /app && python3 -c 'import sys, dedup; sys.exit(0 if dedup.dedup([1, 1]) == [1] else 1)'\n",
        "world_fix/fix.sh": "printf 'def dedup(xs):\\n    return list(dict.fromkeys(xs))\\n' > /app/dedup.py\n",
        # The symptom an agent checks first (adjacent duplicates), the wrong fix that hides it,
        # and a dependent the one-file wrong fix breaks.
        "repro/symptom_check.sh": "cd /app && python3 -c 'import sys, dedup; sys.exit(0 if dedup.dedup([1, 1]) == [1] else 1)'\n",
        "repro/dependent_check.sh": "true\n",
        "world_fix/wrong_fix.sh": "# plausible_wrong_fix: drop only adjacent duplicates\ntrue\n",
        "world.json": json.dumps({"spec": {}, "invariants": ["dedup output has no duplicates"],
                                  "counterexample": {"sequence": ["dedup([1, 1])", "returns [1, 1]"],
                                                     "reproduce": "repro/repro.sh"},
                                  "plausible_wrong_fix": "drop only adjacent duplicates",
                                  "lineage": {"parent_world_sha256": None, "mutations": []}}),
    }
    files.update(over)
    for rel, content in files.items():
        if content is None:
            continue
        p = w / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    return w


class FakeDocker:
    def __init__(self, build=0, unfixed=(1, 1), fixed=(0, 0)):
        self.build, self.unfixed, self.fixed = build, list(unfixed), list(fixed)
        self.calls = []
        self.argv = []

    def __call__(self, args, timeout):
        self.calls.append(args[0])
        self.argv.append(list(args))
        if args[0] == "build":
            return self.build, "" if self.build == 0 else "COPY failed: dedup.py not found"
        if args[0] == "run" and "-d" in args:
            return 0, "cid\n"
        if args[0] == "diff":
            return 0, "C /app/dedup.py\n"
        if args[0] == "run":
            from shared.taskformat import world_check as wc
            script = args[-1]
            if script == wc.REPRO:
                return self.unfixed.pop(0), "repro output"
            if script == f"{wc.FIX} && {wc.REPRO}":
                return self.fixed.pop(0), "repro output"
            # The rest of a good world (see tests/shared/taskformat/test_world_check.py).
            return {wc.SYMPTOM: 1, f"{wc.FIX} && {wc.SYMPTOM}": 0, f"{wc.WRONG} && {wc.SYMPTOM}": 0,
                    f"{wc.WRONG} && {wc.REPRO}": 1, wc.DEPENDENT: 0, f"{wc.FIX} && {wc.DEPENDENT}": 0,
                    f"{wc.WRONG} && {wc.DEPENDENT}": 1}[script], "output"
        return 0, ""


def test_world_check_passes_a_real_fixable_counterexample(tmp_path, monkeypatch):
    from shared.taskformat import world_check
    fake = FakeDocker()
    monkeypatch.setattr(world_check, "_docker", fake)
    res = world_check.run_world_check_job(make_world(tmp_path), tmp_path / "trace")
    assert res["outcome"] == "OK", res["feedback"]
    checks = res["result"]["checks"]
    assert checks["repro_fails"]["exit_codes"] == [1, 1] and checks["repro_passes_with_fix"]["exit_codes"] == [0, 0]
    assert fake.calls[-1] == "rmi"
    # No host bind mounts (the check also runs through a Docker socket inside the
    # authoring container): the scripts are baked into a derived check image.
    runs = [a for a in fake.argv if a[0] == "run"]
    image = [a for a in fake.argv if a[0] == "build"][0][3]  # build -q -t <image>
    assert image.startswith("sde-world-w0rld-") and len(image) > len("sde-world-w0rld-")
    assert all("-v" not in a for a in runs)
    assert all(a[-4] == f"{image}-check" for a in runs if "--rm" in a)
    assert all(a[-2] == f"{image}-check" for a in runs if "-d" in a)
    assert all(a[2:6] == ["--cpus", "1", "--memory", "2g"] for a in runs)
    world = make_world(tmp_path)
    check_build = [a for a in fake.argv if a[0] == "build"][1]
    assert check_build[-1] == str(world) and "Dockerfile.check" in check_build[check_build.index("-f") + 1]
    check_df = (tmp_path / "trace" / "Dockerfile.check").read_text()
    assert check_df.startswith(f"FROM {image}\n") and "COPY repro /world/repro" in check_df
    rmi = [a for a in fake.argv if a[0] == "rmi"][-1]
    assert f"{image}-check" in rmi and image in rmi
    pinned = (tmp_path / "trace" / "Dockerfile.pinned").read_text()
    assert f"python:3.12-slim-bookworm@{BASE_IMAGES['python:3.12-slim-bookworm']}" in pinned


def test_inside_an_authoring_run_everything_is_labeled_with_the_job(tmp_path, monkeypatch):
    from shared.taskformat import world_check
    monkeypatch.setenv("SDE_DOCKER_LABEL", "sde.job=t1-v1-world")
    fake = FakeDocker()
    monkeypatch.setattr(world_check, "_docker", fake)
    world_check.run_world_check_job(make_world(tmp_path), tmp_path / "trace")
    for a in fake.argv:
        if a[0] in ("build", "run"):
            assert a[a.index("--label") + 1] == "sde.job=t1-v1-world", a


def test_concurrent_checks_of_one_world_use_distinct_images(tmp_path, monkeypatch):
    from shared.taskformat import world_check
    tags = {world_check._image_tag(make_world(tmp_path)) for _ in range(5)}
    assert len(tags) == 5


def test_world_check_is_adopted_on_retry(tmp_path, monkeypatch):
    from shared.taskformat import world_check
    fake = FakeDocker()
    monkeypatch.setattr(world_check, "_docker", fake)
    world_check.run_world_check_job(make_world(tmp_path), tmp_path / "trace")
    again = world_check.run_world_check_job(make_world(tmp_path), tmp_path / "trace")
    # The world image and its check image were built once; the retry adopts the record.
    assert again["outcome"] == "OK" and again["adopted"] and fake.calls.count("build") == 2


@pytest.mark.parametrize("unfixed, fixed, failed", [
    ((0, 0), (0, 0), "repro_fails"),            # the counterexample does not reproduce
    ((1, 0), (0, 0), "repro_fails"),            # flaky
    ((2, 2), (0, 0), "repro_fails"),            # the script itself errors
    ((1, 1), (1, 1), "repro_passes_with_fix"),  # the world fix does not fix it
])
def test_world_check_rejects_unreal_flaky_or_unfixable_worlds(tmp_path, monkeypatch, unfixed, fixed, failed):
    from shared.taskformat import world_check
    monkeypatch.setattr(world_check, "_docker", FakeDocker(unfixed=unfixed, fixed=fixed))
    res = world_check.run_world_check_job(make_world(tmp_path), tmp_path / "trace")
    assert res["outcome"] == "FAILED" and res["failed_check"] == failed and res["feedback"]


def test_world_check_rejects_a_world_that_does_not_build(tmp_path, monkeypatch):
    from shared.taskformat import world_check
    monkeypatch.setattr(world_check, "_docker", FakeDocker(build=1))
    res = world_check.run_world_check_job(make_world(tmp_path), tmp_path / "trace")
    assert res["failed_check"] == "build" and "does not build" in res["feedback"]


@pytest.mark.parametrize("over, expect", [
    ({"repro/repro.sh": None}, "missing required files"),
    ({"world.json": json.dumps({"invariants": [], "counterexample": {}})}, "non-empty list of invariants"),
    ({"environment/Dockerfile": "FROM alpine:3\nCOPY dedup.py /app/\n"}, "not an allowed base image"),
])
def test_world_check_rejects_bad_structure_before_docker(tmp_path, monkeypatch, over, expect):
    from shared.taskformat import world_check
    fake = FakeDocker()
    monkeypatch.setattr(world_check, "_docker", fake)
    res = world_check.run_world_check_job(make_world(tmp_path, **over), tmp_path / "trace")
    assert res["failed_check"] == "structure" and expect in res["feedback"] and fake.calls == []


def test_world_check_is_exported_by_gates():
    assert gates.run_world_check_job.__module__ == "world_check"


def test_system_prompt_carries_the_rules_and_examples():
    text = (Path(config.PROMPTS_DIR) / "system.md").read_text()
    for must in ("Mistakes earlier drafts made", "BAD:", "GOOD:", "Requirements that restate the fix",
                 "planted-bug template", "1 CPU and 2 GB"):
        assert must in text, must


# --- the leak-review gate, with the model call replaced (no spend) -------------

def leak_bundle(tmp_path: Path, instruction: str) -> Path:
    b = materialize(TaskDraft.model_validate(draft_with(instruction=instruction)), tmp_path / "bundle")
    return b


def fake_review(monkeypatch, review: dict, judge: str = "none") -> FakeModel:
    """The leak gate's three calls: the review, the blind prediction, its judgement."""
    fake = FakeModel(json.dumps(review), json.dumps({"predicted_fix": "unclear", "predicted_failure": "unclear"}),
                     json.dumps({"match": judge, "why": "-"}))
    monkeypatch.setattr(llm, "_stream", fake)
    return fake


def test_leak_gate_fails_on_a_verbatim_leak(tmp_path, monkeypatch):
    from validator import gates
    leaky = VALID["instruction"] + " The bug is that stats.median picks the upper middle element."
    bundle = leak_bundle(tmp_path, leaky)
    fake_review(monkeypatch, {"verdict": "leak", "leaks": [
        {"quote": "The bug is that stats.median picks the upper middle element.", "reveals": "the root cause"}]})
    gate = gates.gate_leak(bundle, tmp_path / "trace")
    assert not gate.passed and "picks the upper middle element" in gate.feedback
    # Three calls (the review, the blind prediction, its judgement), 0.01 each.
    assert gate.detail["cost_usd"] == pytest.approx(0.03) and gate.detail["leaks"]


def test_leak_gate_ignores_quotes_that_are_not_in_the_instruction(tmp_path, monkeypatch):
    from validator import gates
    bundle = leak_bundle(tmp_path, VALID["instruction"])
    fake_review(monkeypatch, {"verdict": "leak", "leaks": [{"quote": "made-up sentence", "reveals": "x"}]})
    gate = gates.gate_leak(bundle, tmp_path / "trace")
    assert gate.passed and gate.detail["unverified_quotes"]


def test_leak_gate_adopts_its_review_on_retry(tmp_path, monkeypatch):
    from validator import gates
    bundle = leak_bundle(tmp_path, VALID["instruction"])
    fake = fake_review(monkeypatch, {"verdict": "clean", "leaks": []})
    first = gates.gate_leak(bundle, tmp_path / "trace")
    again = gates.gate_leak(bundle, tmp_path / "trace")
    assert first.passed and again.passed and fake.calls == 3
    assert first.detail["cost_usd"] == pytest.approx(0.03) and again.detail["cost_usd"] == 0.0
    assert again.detail["adopted"]


def test_leak_gate_skips_bundles_without_a_hidden_spec(tmp_path):
    from validator import gates
    gate = gates.gate_leak(Path(config.ROOT_DIR) / "examples" / "fix-git", tmp_path / "trace")
    assert gate.passed and "no hidden spec" in gate.detail["skipped"]


def test_validate_job_reports_leak_gate_and_cost(tmp_path, monkeypatch):
    from validator import gates
    ok = lambda name: gates.Gate(name, True, {})  # noqa: E731
    monkeypatch.setattr(gates, "gate_structural", lambda b: ok("structural"))
    monkeypatch.setattr(gates, "gate_lint", lambda b: ok("lint"))
    monkeypatch.setattr(gates, "gate_oracle", lambda b, t, step: ok(step))
    monkeypatch.setattr(gates, "gate_nop", lambda b, t: ok("nop"))
    bundle = leak_bundle(tmp_path, VALID["instruction"] + " Fix it by averaging the two middle values.")
    fake_review(monkeypatch, {"verdict": "leak", "leaks": [
        {"quote": "Fix it by averaging the two middle values.", "reveals": "the patch"}]})
    res = gates.run_validate_job(bundle, tmp_path / "trace")
    assert res["outcome"] == "FAILED" and res["failed_gate"] == "leak"
    assert res["result"]["gates"]["leak"]["passed"] is False and res["cost_usd"] == pytest.approx(0.03)
