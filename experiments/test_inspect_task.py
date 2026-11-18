import json
from pathlib import Path

import inspect_task

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


def test_audit_catches_the_hidden_login_in_configure_git_webserver():
    r = inspect_task.audit(EXAMPLES / "configure-git-webserver")
    assert r["verdict"].startswith("SUSPECT")
    assert {(x["kind"], x["value"]) for x in r["unstated"]} == {
        ("user@host", "git@localhost"), ("credential", "password")}


def test_audit_passes_examples_that_state_what_they_test():
    for name in ("openssl-selfsigned-cert", "log-summary-date-ranges", "fix-code-vulnerability"):
        assert inspect_task.audit(EXAMPLES / name)["verdict"] == "OK", name


def test_audit_accepts_values_stated_in_prose_or_provided_by_the_environment(tmp_path):
    (tmp_path / "environment").mkdir()
    (tmp_path / "environment" / "Dockerfile").write_text("RUN useradd deploy && echo deploy:s3cret | chpasswd\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_outputs.py").write_text(
        'URL = "http://127.0.0.1:9090/status"\n'
        'login("deploy", "s3cret")\n'
        'OUT = "/app/out/report.csv"\n')
    (tmp_path / "instruction.md").write_text("Serve /status on port 9090 and write /app/out/report.csv.\n")
    assert inspect_task.audit(tmp_path)["verdict"] == "OK"


HAPPY = [{"type": "acquire", "worker": "A"}, {"type": "send", "worker": "A"}, {"type": "deliver", "index": 0}]
STALE = [{"type": "acquire", "worker": "B"}, {"type": "expire"}, {"type": "acquire", "worker": "A"},
         {"type": "send", "worker": "A"}, {"type": "send", "worker": "B"}, {"type": "deliver", "index": 0}]
RULES = ({"acquire", "send", "deliver"}, ("not lower than the highest",))


def _leaky_task(tmp_path: Path, examples: dict, instruction: str) -> Path:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_outputs.py").write_text(f"HAPPY = {HAPPY!r}\nSTALE = {STALE!r}\n")
    (tmp_path / "environment" / "examples").mkdir(parents=True)
    for name, steps in examples.items():
        (tmp_path / "environment" / "examples" / name).write_text(json.dumps(steps))
    (tmp_path / "instruction.md").write_text(instruction)
    return tmp_path


def test_leaks_catches_shipped_reproductions_and_a_stated_fix(tmp_path):
    task = _leaky_task(tmp_path, {"happy.json": HAPPY, "stale.json": STALE},
                       "Accept a write whose token is not lower than the highest accepted one.")
    r = inspect_task.leaks(task, *RULES)
    kinds = sorted((f["kind"], f["file"]) for f in r["leaks"])
    assert kinds == [("failure_scenario", "environment/examples/stale.json"),
                     ("hidden_scenario", "environment/examples/stale.json"),
                     ("states_the_fix", "instruction.md")]
    assert "stale.json" in inspect_task.leak_feedback(r)


def test_leaks_allows_a_happy_path_example_and_outcome_only_requirements(tmp_path):
    task = _leaky_task(tmp_path, {"happy.json": HAPPY},
                       "A write under an older lease never takes effect after a newer lease's write.")
    assert inspect_task.leaks(task, *RULES)["verdict"] == "OK"


def test_leaks_rejects_the_v1_tla_task_if_present():
    bundle = (Path(__file__).resolve().parent.parent / "work" / "bundles"
              / "0a4f71ce2e177c37582a53b79ace85b1b838cb16fbfb3db76af6bc7698609a92")
    if not bundle.exists():
        import pytest

        pytest.skip("tla1 bundle not on this machine")
    r = inspect_task.leaks(bundle, *RULES)
    assert {f["kind"] for f in r["leaks"]} == {"failure_scenario", "hidden_scenario", "states_the_fix"}


def _trial(tmp_path: Path, reward: float, analyses: list[str], terminal: str = "") -> Path:
    trial = tmp_path / "trial"
    (trial / "agent").mkdir(parents=True)
    (trial / "verifier").mkdir()
    steps = [{"step_id": 0, "source": "user", "message": "task"}] + [
        {"step_id": i, "source": "agent", "message": a,
         "tool_calls": [{"arguments": {"keystrokes": "ls\n"}}],
         "observation": {"results": [{"content": terminal}]}} for i, a in enumerate(analyses, 1)]
    (trial / "agent" / "trajectory.json").write_text(json.dumps({"steps": steps}))
    (trial / "result.json").write_text(json.dumps({"verifier_result": {"rewards": {"reward": reward}}}))
    (trial / "verifier" / "test-stdout.txt").write_text("FAILED tests/test_outputs.py::test_x - AssertionError\n")
    return trial


def test_explain_flags_a_run_the_agent_saw_working_as_a_suspect_task(tmp_path):
    r = inspect_task.explain(_trial(tmp_path, 0.0, [
        "Installing packages.", "The complete workflow was validated end to end: curl returned hello world."]))
    assert r["verdict"].startswith("SUSPECT TASK") and "self_verified_but_failed" in r["flags"]
    assert r["failing_tests"] == ["tests/test_outputs.py::test_x - AssertionError"]


def test_explain_flags_mechanical_slips_and_ignores_routine_probes(tmp_path):
    r = inspect_task.explain(_trial(tmp_path, 0.0, [
        "The Go toolchain exists at /usr/local/go/bin but is not on PATH.",
        "The edit did not occur."], terminal="bash: nginx: command not found\nno server running on /tmp/tmux-1000"))
    assert r["verdict"].startswith("MECHANICAL")
    assert set(r["flags"]) == {"missing_tool", "edit_slip", "shell_killed"}

    probe_only = inspect_task.explain(_trial(tmp_path / "b", 0.0, ["Wrong result."],
                                             terminal="bash: nginx: command not found"))
    assert probe_only["verdict"].startswith("MODEL FAILURE") and probe_only["flags"] == []
