"""Tests for running and recovering Harbor trials, with a fake `harbor` binary.

Run with: python -m pytest evaluation
"""

import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from evaluation import harbor, settings

AGENT = harbor.AgentConfig("terminus-2", "openrouter/z-ai/glm-5.3-flash", {"reasoning_effort": "high"})

# Mimics `harbor run`: writes <out>/<job>/<trial>/result.json after FAKE_SLEEP
# seconds, and counts its invocations in FAKE_CALLS.
FAKE_HARBOR = f"""#!{sys.executable}
import json, os, sys, time
args = sys.argv[1:]
job, out = args[args.index("--job-name") + 1], args[args.index("-o") + 1]
with open(os.environ["FAKE_CALLS"], "a") as f:
    f.write(json.dumps({{"pid": os.getpid(), "args": args}}) + "\\n")
time.sleep(float(os.environ.get("FAKE_SLEEP", "0")))
if os.environ.get("FAKE_NO_RESULT"):
    sys.exit(1)
trial = os.path.join(out, job, "task__xyz")
os.makedirs(trial, exist_ok=True)
with open(os.path.join(trial, "result.json"), "w") as f:
    json.dump({{"trial_name": "task__xyz", "finished_at": "2026-11-17T22:09:03Z",
               "verifier_result": {{"rewards": {{"reward": 1.0}}}}}}, f)
"""


@pytest.fixture
def fake(tmp_path, monkeypatch):
    bin_path = tmp_path / "harbor"
    bin_path.write_text(FAKE_HARBOR)
    bin_path.chmod(0o755)
    calls = tmp_path / "calls.jsonl"
    monkeypatch.setattr(settings, "HARBOR_BIN", str(bin_path))
    monkeypatch.setattr(settings, "CANCEL_GRACE_SEC", 2.0)
    monkeypatch.setenv("FAKE_CALLS", str(calls))
    traces = tmp_path / "traces"

    def n_calls() -> int:
        return len(calls.read_text().splitlines()) if calls.exists() else 0

    return traces, n_calls


def run(traces, job_id="t1-v1-rollout1-r0", cancel=None):
    return harbor.run_trial(Path("/tasks/t1"), job_id, AGENT, cancel or threading.Event(),
                            traces_dir=traces, env_file=Path("/nonexistent/.env"), poll_sec=0.05)


def test_command_pins_agent_model_and_reasoning_effort(tmp_path):
    cmd = harbor.command(Path("/tasks/t1"), "job-1", AGENT, tmp_path, env_file=None)
    joined = " ".join(cmd)
    assert "-a terminus-2" in joined and "-m openrouter/z-ai/glm-5.3-flash" in joined
    assert "--ak reasoning_effort=high" in joined
    assert "--n-attempts 1" in joined and "--max-retries 0" in joined and "--job-name job-1" in joined


def test_runs_once_then_adopts_the_finished_trial(fake):
    traces, n_calls = fake
    first = run(traces)
    assert not first.adopted and first.result["verifier_result"]["rewards"]["reward"] == 1.0
    again = run(traces)
    assert again.adopted and n_calls() == 1  # no second Harbor run, no second payment


def test_half_finished_directory_is_set_aside_and_rerun(fake):
    traces, n_calls = fake
    (traces / "t1-v1-rollout1-r0" / "task__old").mkdir(parents=True)  # killed before result.json
    run(traces)
    assert (traces / "t1-v1-rollout1-r0.abandoned-1" / "task__old").is_dir()
    assert n_calls() == 1


def test_a_trial_we_cancelled_at_shutdown_is_rerun_not_adopted(fake):
    traces, n_calls = fake
    trial = traces / "t1-v1-rollout1-r0" / "task__old"
    trial.mkdir(parents=True)
    (trial / "result.json").write_text(json.dumps({
        "trial_name": "task__old", "finished_at": "2026-11-17T22:29:16Z", "verifier_result": None,
        "exception_info": {"exception_type": "CancelledError", "exception_message": ""}}))
    found = run(traces)
    assert not found.adopted and n_calls() == 1
    assert (traces / "t1-v1-rollout1-r0.abandoned-1" / "task__old").is_dir()


def test_exit_without_result_raises(fake, monkeypatch):
    traces, _ = fake
    monkeypatch.setenv("FAKE_NO_RESULT", "1")
    with pytest.raises(harbor.HarborError):
        run(traces)


def test_cancel_stops_harbor(fake, monkeypatch):
    traces, _ = fake
    monkeypatch.setenv("FAKE_SLEEP", "30")
    cancel = threading.Event()
    threading.Timer(0.5, cancel.set).start()
    started = time.monotonic()
    with pytest.raises(harbor.Cancelled):
        run(traces, cancel=cancel)
    assert time.monotonic() - started < 10
    assert not (traces / "t1-v1-rollout1-r0.pid").exists()
    pid = json.loads((traces.parent / "calls.jsonl").read_text().splitlines()[0])["pid"]
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_waits_for_and_adopts_a_run_left_behind_by_a_dead_worker(fake, monkeypatch, tmp_path):
    traces, n_calls = fake
    traces.mkdir(parents=True)
    # A previous worker started Harbor and died; Harbor is still running.
    env = {**os.environ, "FAKE_SLEEP": "1"}
    orphan = subprocess.Popen([settings.HARBOR_BIN, "run", "--job-name", "t1-v1-rollout1-r0", "-o", str(traces)],
                              env=env)
    (traces / "t1-v1-rollout1-r0.pid").write_text(json.dumps({"pid": orphan.pid, "host": socket.gethostname()}))
    reaper = threading.Thread(target=orphan.wait)  # stands in for init reaping the orphan
    reaper.start()
    found = run(traces)
    reaper.join()
    assert found.adopted and n_calls() == 1
    assert not (traces / "t1-v1-rollout1-r0.pid").exists()


def test_a_reused_pid_is_not_mistaken_for_the_jobs_harbor_run(fake):
    traces, n_calls = fake
    traces.mkdir(parents=True)
    # The pid file points at a live process that is not this job's Harbor (this test runner).
    (traces / "t1-v1-rollout1-r0.pid").write_text(json.dumps({"pid": os.getpid(), "host": socket.gethostname()}))
    found = run(traces)
    assert not found.adopted and n_calls() == 1
