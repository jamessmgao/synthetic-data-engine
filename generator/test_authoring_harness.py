"""authoring_harness: render, run (fake Harbor), adopt, cost from tokens."""

import json
import subprocess
from pathlib import Path

import pytest

from generator import authoring_harness as ah
from shared.contracts import config


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
        (trial / "artifacts").mkdir(parents=True)
        for rel in artifacts:
            (trial / "artifacts" / rel).parent.mkdir(parents=True, exist_ok=True)
            (trial / "artifacts" / rel).write_text("{}")
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
