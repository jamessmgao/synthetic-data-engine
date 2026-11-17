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


def test_generate_job_reports_bad_input(tmp_path):
    res = run_generate_job("x", {"origin": "repair", "bundle_sha256": "nope", "feedback": ""}, tmp_path)
    assert res["outcome"] == "FAILED" and "bad job input" in res["error"]
