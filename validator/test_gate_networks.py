"""run_harbor_step removes its trial's compose network, also when Harbor times out."""

import json
import subprocess
from pathlib import Path

import pytest

from validator import gates


def fake_harbor(calls, timeout=False):
    def run(cmd, **kw):
        calls.append(cmd)
        if cmd[0] == "docker":
            return subprocess.CompletedProcess(cmd, 0, "", "")
        trial = Path(cmd[cmd.index("-o") + 1]) / cmd[cmd.index("--job-name") + 1] / "task__AbC"
        trial.mkdir(parents=True)
        if timeout:
            raise subprocess.TimeoutExpired(cmd, 1)
        (trial / "result.json").write_text(json.dumps({"verifier_result": {"rewards": {"reward": 1}}}))
        (trial.parent / "result.json").write_text("{}")
        return subprocess.CompletedProcess(cmd, 0, "", "")
    return run


def network_rms(calls):
    return [c for c in calls if c[:3] == ["docker", "network", "rm"]]


def test_a_gate_trial_removes_its_network(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(gates.subprocess, "run", fake_harbor(calls))
    out = gates.run_harbor_step(tmp_path / "bundle", "oracle", tmp_path / "trace", "oracle1")
    assert out.reward == 1
    assert network_rms(calls) == [["docker", "network", "rm", "task__abc__env_default"]]


def test_a_timed_out_gate_trial_still_removes_its_network(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(gates.subprocess, "run", fake_harbor(calls, timeout=True))
    with pytest.raises(subprocess.TimeoutExpired):
        gates.run_harbor_step(tmp_path / "bundle", "nop", tmp_path / "trace", "nop")
    assert network_rms(calls) == [["docker", "network", "rm", "task__abc__env_default"]]
