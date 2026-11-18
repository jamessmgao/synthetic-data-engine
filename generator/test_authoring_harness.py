"""authoring_harness: render, run (fake Harbor), adopt, cost from tokens."""

import json
import subprocess
from pathlib import Path

import pytest

from generator import authoring_harness as ah
from shared.contracts import config


class RunPopen:
    """`harbor run` is started with Popen; this one hands it to whatever fake subprocess.run
    the test installed (so the fakes below serve both), and is finished at once."""

    def __init__(self, cmd, **kw):
        self.args, self.pid, self.returncode = cmd, -1, 0
        try:
            subprocess.run(cmd, **{k: v for k, v in kw.items() if k == "env"})
        except subprocess.TimeoutExpired:
            pass  # a fake for a run that never finished: no result, like a stopped trial

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode


@pytest.fixture(autouse=True)
def popen(monkeypatch):
    monkeypatch.setattr(ah.subprocess, "Popen", RunPopen)


def fake_harbor(calls: list, *, exception=None, artifacts=("world/world.json",), tokens=(200_000, 10_000)):
    """Stands in for `harbor run` (writes a trial with result.json and artifacts/)
    and for the docker cleanup calls (recorded, answered as if 2 containers remain)."""

    def run(cmd, **kw):
        if cmd[0] == "docker":
            calls.append({"docker": cmd})
            out = "c1\nc2\n" if cmd[1] == "ps" else ""
            return subprocess.CompletedProcess(cmd, 0, out, "")
        calls.append({"cmd": cmd, "env": kw.get("env")})
        trace = Path(cmd[cmd.index("-o") + 1])
        trial = trace / cmd[cmd.index("--job-name") + 1] / "authoring__abc"
        # Harbor 0.26's real layout: /logs/artifacts is mirrored under artifacts/logs/artifacts,
        # with a manifest saying so (tests that wrote artifacts/ directly hid a real bug).
        copy = trial / "artifacts" / "logs" / "artifacts"
        copy.mkdir(parents=True)
        (trial / "artifacts" / "manifest.json").write_text(json.dumps(
            [{"source": "/logs/artifacts", "destination": "artifacts/logs/artifacts", "status": "ok"}]))
        for rel in artifacts:
            (copy / rel).parent.mkdir(parents=True, exist_ok=True)
            (copy / rel).write_text("{}")
        (trial / "result.json").write_text(json.dumps({
            "agent_result": {"n_input_tokens": tokens[0], "n_output_tokens": tokens[1], "cost_usd": 5e-05,
                             "metadata": {"n_episodes": 42}},
            "agent_execution": {"started_at": "2026-11-18T01:00:00+00:00", "finished_at": "2026-11-18T01:20:00+00:00"},
            "exception_info": exception,
        }))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    return run


def test_render_task_is_a_valid_harbor_task(tmp_path, monkeypatch):
    from harbor.models.task.task import Task

    monkeypatch.setenv("SDE_AUTHORING_IMAGE", "sde-authoring:abc123")
    task = ah.render_task(tmp_path / "task", "t1-v1-world", "Build a world.", {"world/world.json": b"{}"}, 3600)
    assert Task.is_valid_dir(task)
    assert (task / "environment" / "Dockerfile").read_text().startswith("FROM sde-authoring:abc123\n")
    assert "/var/run/docker.sock:/var/run/docker.sock" in (task / "environment" / "docker-compose.yaml").read_text()
    assert (task / "environment" / "input" / "world" / "world.json").read_bytes() == b"{}"
    assert "timeout_sec = 3600.0" in (task / "task.toml").read_text()


def test_render_task_refuses_paths_that_escape_input(tmp_path):
    with pytest.raises(ValueError):
        ah.render_task(tmp_path / "task", "j", "x", {"../../etc/passwd": b"x"}, 60)


def test_runs_terminus_with_glm51_and_costs_from_tokens(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(ah.subprocess, "run", fake_harbor(calls))
    out = ah.run_authoring_trial("t1-v1-world", "Build a world.", {}, tmp_path)
    cmd = calls[0]["cmd"]
    docker = [c["docker"] for c in calls if "docker" in c]
    assert docker[0] == ["docker", "ps", "-sde", "--filter", "label=sde.job=t1-v1-world"]
    assert docker[1] == ["docker", "rm", "-f", "c1", "c2"]
    assert docker[2] == ["docker", "image", "prune", "-af", "--filter", "label=sde.job=t1-v1-world"]
    toml = (tmp_path / "traces" / "t1-v1-world" / "task" / "task.toml").read_text()
    assert 'SDE_DOCKER_LABEL = "sde.job=t1-v1-world"' in toml
    assert cmd[cmd.index("-a") + 1] == "terminus-2" and cmd[cmd.index("-m") + 1] == f"openrouter/{config.MODEL}"
    assert cmd[cmd.index("-o") + 1] == str(tmp_path / "traces" / "t1-v1-world")
    want = 200_000 / 1e6 * config.GENERATOR_PRICE_IN_PER_M + 10_000 / 1e6 * config.GENERATOR_PRICE_OUT_PER_M
    assert out["cost_usd"] == pytest.approx(want) and out["gateway_cost_usd"] == 5e-05
    assert out["steps"] == 42 and out["duration_sec"] == 1200 and out["error"] is None and not out["adopted"]
    assert (Path(out["artifacts_dir"]) / "world" / "world.json").is_file()


def test_a_finished_trial_is_adopted_not_rerun(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(ah.subprocess, "run", fake_harbor(calls))
    first = ah.run_authoring_trial("j", "x", {}, tmp_path)
    again = ah.run_authoring_trial("j", "x", {}, tmp_path)
    runs = [c for c in calls if "cmd" in c]
    assert len(runs) == 1 and again["adopted"] and again["artifacts_dir"] == first["artifacts_dir"]


def test_every_trial_reports_its_instruction_size(tmp_path, monkeypatch):
    monkeypatch.setattr(ah.subprocess, "run", fake_harbor([]))
    text = "Build a world. " * 100
    ran = ah.run_authoring_trial("j", text, {}, tmp_path)
    adopted = ah.run_authoring_trial("j", text, {}, tmp_path)
    assert ran["instruction_size"] == adopted["instruction_size"] == {"chars": 1500, "tokens_est": 375}


def test_a_half_finished_run_is_kept_aside_and_rerun(tmp_path, monkeypatch):
    stale = tmp_path / "traces" / "j" / "authoring" / "authoring__dead"
    stale.mkdir(parents=True)
    (stale / "agent.log").write_text("killed")
    calls = []
    monkeypatch.setattr(ah.subprocess, "run", fake_harbor(calls))
    out = ah.run_authoring_trial("j", "x", {}, tmp_path)
    assert len([c for c in calls if "cmd" in c]) == 1 and not out["adopted"]
    assert (tmp_path / "traces" / "j" / "authoring.abandoned-1" / "authoring__dead" / "agent.log").is_file()


def test_harbor_exceptions_and_missing_results_are_reported(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(ah.subprocess, "run", fake_harbor(
        calls, exception={"exception_type": "AgentTimeoutError", "exception_message": "60 min"}))
    out = ah.run_authoring_trial("a", "x", {}, tmp_path)
    assert out["exception_type"] == "AgentTimeoutError" and "AgentTimeoutError" in out["error"]

    monkeypatch.setattr(ah.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", "boom"))
    out = ah.run_authoring_trial("b", "x", {}, tmp_path)
    assert out["exception_type"] == "NoTrialResult" and out["artifacts_dir"] is None and out["cost_usd"] == 0.0


def test_cleanup_runs_even_when_harbor_times_out(tmp_path, monkeypatch):
    seen = []

    def run(cmd, **kw):
        seen.append(cmd[0] if cmd[0] != "docker" else " ".join(cmd[:3]))
        if cmd[0] != "docker":
            raise subprocess.TimeoutExpired(cmd, 1)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(ah.subprocess, "run", run)
    out = ah.run_authoring_trial("slow", "x", {}, tmp_path)
    assert out["exception_type"] == "NoTrialResult" and "docker image prune" in seen


def test_artifacts_dir_follows_the_manifest_then_the_known_layouts(tmp_path):
    trial = tmp_path / "trial"
    (trial / "artifacts" / "elsewhere").mkdir(parents=True)
    (trial / "artifacts" / "manifest.json").write_text(json.dumps(
        [{"source": "/var/log/x", "destination": "artifacts/var"},
         {"source": "/logs/artifacts/", "destination": "artifacts/elsewhere"}]))
    assert ah.artifacts_dir(trial) == trial / "artifacts" / "elsewhere"
    (trial / "artifacts" / "manifest.json").unlink()
    (trial / "artifacts" / "logs" / "artifacts").mkdir(parents=True)
    assert ah.artifacts_dir(trial) == trial / "artifacts" / "logs" / "artifacts"
    other = tmp_path / "flat"
    (other / "artifacts").mkdir(parents=True)
    assert ah.artifacts_dir(other) == other / "artifacts"


# --- the command, and stopping a running trial ---------------------------------------------


def test_reasoning_effort_is_passed_only_when_set(tmp_path, monkeypatch):
    monkeypatch.delenv("SDE_AUTHORING_REASONING_EFFORT", raising=False)
    assert "--ak" not in ah.harbor_command(tmp_path, tmp_path)
    monkeypatch.setenv("SDE_AUTHORING_REASONING_EFFORT", "medium")
    cmd = ah.harbor_command(tmp_path, tmp_path)
    assert cmd[cmd.index("--ak") + 1] == "reasoning_effort=medium" and cmd[:4] == [config.HARBOR_BIN, "run", "-p", str(tmp_path)]


def test_llm_timeout_and_max_tokens_go_to_terminus_as_llm_kwargs(tmp_path, monkeypatch):
    for var in ("SDE_AUTHORING_REASONING_EFFORT", "SDE_AUTHORING_LLM_TIMEOUT_S", "SDE_AUTHORING_MAX_TOKENS"):
        monkeypatch.delenv(var, raising=False)
    assert ah.llm_kwargs() == {} and "--ak" not in ah.harbor_command(tmp_path, tmp_path)  # unset: unchanged
    monkeypatch.setenv("SDE_AUTHORING_LLM_TIMEOUT_S", "300")
    monkeypatch.setenv("SDE_AUTHORING_MAX_TOKENS", "32000")
    cmd = ah.harbor_command(tmp_path, tmp_path)
    # Harbor JSON-parses --ak values (harbor/cli/utils.py:parse_kwargs) into terminus-2's llm_kwargs.
    assert cmd[-2:] == ["--ak", 'llm_kwargs={"max_tokens": 32000, "timeout": 300.0}']
    monkeypatch.setenv("SDE_AUTHORING_MAX_TOKENS", "0")
    with pytest.raises(ValueError):
        ah.llm_kwargs()
    monkeypatch.setenv("SDE_AUTHORING_MAX_TOKENS", "lots")
    with pytest.raises(ValueError):
        ah.llm_kwargs()


def _real_trial(monkeypatch, tmp_path, cmd):
    """run_authoring_trial over a real process (`cmd` instead of Harbor), fast polling."""
    import threading

    monkeypatch.setattr(ah.subprocess, "Popen", REAL_POPEN)
    monkeypatch.setattr(ah, "harbor_command", lambda task_dir, trace: cmd)
    monkeypatch.setattr(ah, "POLL_SEC", 0.05)
    monkeypatch.setattr(ah, "STOP_GRACE_SEC", 2.0)
    cleaned = []
    monkeypatch.setattr(ah, "cleanup_docker", lambda job_id: cleaned.append(job_id))
    out: dict = {}
    t = threading.Thread(target=lambda: out.update(ah.run_authoring_trial("j1", "x", {}, tmp_path, timeout_sec=60)))
    t.start()
    return t, out, cleaned


REAL_POPEN = subprocess.Popen


def test_stop_trial_kills_the_running_harbor_and_cleans_up(tmp_path, monkeypatch):
    import time

    t, out, cleaned = _real_trial(monkeypatch, tmp_path, ["sleep", "60"])
    for _ in range(100):
        if "j1" in ah._RUNNING:
            break
        time.sleep(0.02)
    started = time.monotonic()
    assert ah.stop_trial("j1") is True
    t.join(timeout=10)
    assert not t.is_alive() and time.monotonic() - started < 5  # stopped, not left to its timeout
    assert out["exception_type"] == "Cancelled" and cleaned == ["j1"] and "j1" not in ah._RUNNING
    assert ah.stop_trial("j1") is False  # nothing running any more


def test_a_trial_past_its_deadline_is_stopped(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "BUILD_TIMEOUT_SEC", -959.5)  # deadline = 60 + build + 900 = 0.5 s
    t, out, cleaned = _real_trial(monkeypatch, tmp_path, ["sleep", "60"])
    t.join(timeout=10)
    assert not t.is_alive() and out["exception_type"] == "NoTrialResult" and cleaned == ["j1"]


def test_the_trials_compose_network_is_removed_after_the_run(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(ah.subprocess, "run", fake_harbor(calls))
    ah.run_authoring_trial("j", "x", {}, tmp_path)
    removed = [c["docker"] for c in calls if "docker" in c and c["docker"][1:3] == ["network", "rm"]]
    # The fake trial dir is authoring__abc: Harbor's compose project is authoring__abc__env.
    assert removed == [["docker", "network", "rm", "authoring__abc__env_default"]]


def test_remove_trial_networks_names_every_trial_dir(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(ah.subprocess, "run", lambda cmd, **kw: seen.append(cmd))
    job = tmp_path / "authoring"
    (job / "task__FxDKUvk").mkdir(parents=True)
    (job / "task__iibRdZX").mkdir()
    (job / "result.json").write_text("{}")  # files are not trials
    assert ah.remove_trial_networks(job) == ["task__fxdkuvk__env_default", "task__iibrdzx__env_default"]
    assert seen == [["docker", "network", "rm", n] for n in ("task__fxdkuvk__env_default", "task__iibrdzx__env_default")]
    assert ah.remove_trial_networks(tmp_path / "missing") == []
