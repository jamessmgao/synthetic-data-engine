"""
Scripted stand-ins for the three external dependencies of the pipeline. No
model endpoint and no Docker is touched; everything else is the real code.

| Real                                   | Fake                    | Plugged in at                  |
|----------------------------------------|-------------------------|--------------------------------|
| GLM-5.1 drafting (run_generate_job)    | `generate`              | shared.jobs.worker.serve          |
| Harbor gates (run_validate_job)        | `validate`              | shared.jobs.worker.serve          |
| Harbor terminus-2 trial (run_trial)    | `run_trial`             | evaluation.executor.Executor   |

What each task does is scripted in SDE_E2E_SCENARIO (JSON), keyed by the task's
suffix (`t01`, ...):

    {"t01": {"versions": {"1": "01010"}},           # 2/5 at v1
     "t02": {"versions": {"1": "11111", "2": "01100"}},
     "t03": {"gate_failures": 3},                   # every validate round fails
     "t04": {"generate": "fail"},                   # or "crash": kills the worker process
     "t05": {"versions": {"1": "1i000"}}}            # slot 2 invalid once, then passes

Rollout marks: 1 pass, 0 fail, i INVALID on the first attempt then pass,
I INVALID on every attempt. A missing version defaults to "01000".

Like the real components, the fakes are idempotent per job id and leave
evidence of every unit of work in <work>/e2e/<kind>/<job_id>.log (`start`,
`finish`, `adopt` lines), so tests can prove nothing paid ran twice:

- generate caches its "model call" by job id, as task-builder's llm cache does;
- run_trial writes a Harbor-shaped trial dir and adopts it on a rerun, as
  evaluation/harbor.py does.
"""

import json
import os
import re
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evaluation import harbor
from shared.storage import Backend

STEP_S = float(os.environ.get("SDE_E2E_STEP_S", "0.05"))  # how long each fake unit of work takes


def scenario_for(task_id: str) -> dict[str, Any]:
    return json.loads(os.environ.get("SDE_E2E_SCENARIO", "{}")).get(task_id.rsplit("-", 1)[1], {})


def _log(work: Path, kind: str, job_id: str, event: str) -> None:
    path = work / "e2e" / kind / f"{job_id}.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(f"{event} {os.getpid()}\n")


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}")
    tmp.write_text(text)
    os.replace(tmp, path)


def _now() -> str:
    return datetime.now(UTC).isoformat()


# --- generate -------------------------------------------------------------------


def generate(backend: Backend):
    work = backend.work_dir

    def run(job: dict[str, Any]) -> dict[str, Any]:
        sc = scenario_for(job["task_id"])
        cache = work / "e2e" / "llm" / f"{job['id']}.json"
        if cache.exists():  # the "model call" finished before a crash: reuse it, pay nothing
            _log(work, "generate", job["id"], "adopt")
            return {**json.loads(cache.read_text()), "cost_usd": 0.0}

        _log(work, "generate", job["id"], "start")
        time.sleep(STEP_S)
        if sc.get("generate") == "crash":  # a poison job: takes its whole worker process down
            os._exit(137)
        if sc.get("generate") == "fail":
            out = {"outcome": "FAILED", "error": "no acceptable draft after 3 rounds", "cost_usd": 0.25}
        else:
            v = str(job["version"])
            with tempfile.TemporaryDirectory() as tmp:
                d = Path(tmp)
                (d / "instruction.md").write_text(f"Fake task {job['task_id']} v{v} ({job['id']})\n")
                (d / "scenario.json").write_text(json.dumps({
                    "pattern": sc.get("versions", {}).get(v, "01000"),
                    "gate_failures": sc.get("gate_failures", 0),
                }))
                sha = backend.artifacts.put_bundle(d)
            name = f"fake-{job['task_id'].rsplit('-', 1)[1]}"
            out = {"outcome": "OK", "bundle_sha256": sha, "cost_usd": 0.25,
                   "result": {"name": name, "bundle_sha256": sha, "llm_calls": []}}
        _write_atomic(cache, json.dumps(out))
        _log(work, "generate", job["id"], "finish")
        return out

    return run


# --- validate -------------------------------------------------------------------


def validate(backend: Backend):
    work = backend.work_dir

    def run(job: dict[str, Any]) -> dict[str, Any]:
        _log(work, "validate", job["id"], "start")
        time.sleep(STEP_S)
        sha = job["bundle_sha256"] or job["input"]["bundle_sha256"]
        sc = json.loads((backend.artifacts.bundle_path(sha) / "scenario.json").read_text())
        round_ = int(job["id"].rsplit("-r", 1)[1])
        ok = round_ >= sc["gate_failures"]
        gates = {g: {"passed": True} for g in ("structural", "lint", "oracle1", "oracle2", "nop")}
        if not ok:
            gates["oracle1"] = {"passed": False, "reward": 0.0}
            for g in ("oracle2", "nop"):
                gates.pop(g)
        _log(work, "validate", job["id"], "finish")
        feedback = "" if ok else "oracle1: reference solution scored 0 (tests/test_outputs.py::test_crash_recovery)"
        return {"outcome": "OK" if ok else "FAILED", "bundle_sha256": sha, "error": feedback or None,
                "result": {"gates": gates, "feedback": feedback, "failed_gate": None if ok else "oracle1"},
                "trace_uri": backend.artifacts.save_trace(job["id"])}

    return run


# --- Harbor trial ---------------------------------------------------------------

_ROLLOUT_ID = re.compile(r"-rollout(\d+)-r(\d+)$")


def run_trial(task_dir: Path, job_id: str, agent: harbor.AgentConfig, cancel, traces_dir: Path,
              **_: Any) -> harbor.TrialRun:
    work = traces_dir.parent
    job_dir = traces_dir / job_id
    if (found := harbor.finished_trial(job_dir)) is not None:
        _log(work, "trial", job_id, "adopt")
        return found

    _log(work, "trial", job_id, "start")
    started = _now()
    deadline = time.monotonic() + STEP_S
    while time.monotonic() < deadline:
        if cancel.is_set():
            raise harbor.Cancelled(job_id)
        time.sleep(0.01)

    slot, retry = map(int, _ROLLOUT_ID.search(job_id).groups())
    mark = json.loads((task_dir / "scenario.json").read_text())["pattern"][slot - 1]
    invalid = mark == "I" or (mark == "i" and retry == 0)
    passed = mark in "1i"

    trial_dir = job_dir / f"{job_id}__trial"
    stdout = "PASSED tests/test_outputs.py::test_happy_path\n"
    if not invalid and not passed:
        stdout += f"FAILED tests/test_outputs.py::test_crash_recovery - AssertionError: slot {slot} lost a job\n"
    _write_atomic(trial_dir / "verifier" / "test-stdout.txt", stdout)
    result = {
        "trial_name": trial_dir.name,
        "task_checksum": task_dir.name,
        "started_at": started,
        "finished_at": _now(),
        "config": {"agent": {"name": agent.agent, "model_name": agent.model, "kwargs": agent.kwargs}},
        "agent_result": {"cost_usd": 0.012, "n_input_tokens": 90_000, "n_output_tokens": 4_000},
        "verifier_result": None if invalid else {"rewards": {"reward": 1.0 if passed else 0.0}},
        "exception_info": {"exception_type": "AgentTimeoutError", "exception_message": "fake timeout"}
        if invalid else None,
    }
    # result.json last and atomically: a crash before this line leaves nothing to adopt.
    _write_atomic(trial_dir / "result.json", json.dumps(result))
    _log(work, "trial", job_id, "finish")
    return harbor.TrialRun(result=result, trial_dir=trial_dir, adopted=False)
