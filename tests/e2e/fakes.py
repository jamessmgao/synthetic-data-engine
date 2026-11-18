"""
Scripted stand-ins for the three external dependencies of the pipeline. No
model endpoint and no Docker is touched; everything else is the real code.

| Real                                   | Fake                    | Plugged in at                         |
|----------------------------------------|-------------------------|---------------------------------------|
| GLM-5.1 drafting (run_generate_job,    | `generate`              | shared.jobs.worker.serve_generation      |
|   run_create_job from a world)         |                         |                                       |
| World authoring (run_world_job)        | `world`                 | shared.jobs.worker.serve_generation      |
| Harbor gates (run_validate_job)        | `validate`              | shared.jobs.worker.serve_validate        |
| World check (run_world_check_job)      | `world_check`           | shared.jobs.worker.serve_validate        |
| Harbor terminus-2 trial (run_trial)    | `run_trial`             | evaluation.executor.Executor          |
| GLM-5.1 failure review (llm.complete)  | `review`                | evaluation.trace_parser_service       |

What each task does is scripted in SDE_E2E_SCENARIO (JSON), keyed by the task's
suffix (`t01`, ...):

    {"t01": {"versions": {"1": "01010"}},           # 2/5 at v1
     "t02": {"versions": {"1": "11111", "2": "01100"}},
     "t03": {"gate_failures": 3},                   # every validate round fails
     "t04": {"generate": "fail"},                   # or "crash": kills the worker process
     "t05": {"versions": {"1": "1i000"}},            # slot 2 invalid once, then passes
     "t11": {"world_failures": 1}}                   # agent mode: the first world check fails

Rollout marks: 1 pass, 0 fail, i INVALID on the first attempt then pass,
I INVALID on every attempt. A missing version defaults to "01000".

Like the real components, the fakes are idempotent per job id and leave
evidence of every unit of work in <work>/e2e/<kind>/<job_id>.log (`start`,
`finish`, `adopt` lines), so tests can prove nothing paid ran twice:

- generate caches its "model call" by job id, as task-builder's llm cache does;
- run_trial writes a Harbor-shaped trial dir and adopts it on a rerun, as
  evaluation/harbor.py does.
"""

import hashlib
import json
import os
import re
import shutil
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
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
            world = (job["input"] or {}).get("world_sha256")
            if world:  # agent mode: the task is written from a checked world, fetched like the real one
                assert (backend.artifacts.world_path(world) / "world.json").is_file(), world
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
                   "result": {"name": name, "bundle_sha256": sha, "llm_calls": [],
                              **({"world_sha256": world} if world else {})}}
            # The create job's leak review (N6): the draft was written but its instruction
            # gives the fix away, for the first `create_leak` rounds → repaired, not rejected.
            round_ = int(m.group(1)) if (m := re.search(r"-repair(\d+)$", job["id"])) else 0
            if round_ < sc.get("create_leak", 0):
                feedback = "leak: the instruction says 'take the row lock before reading stock'"
                out = {"outcome": "FAILED", "bundle_sha256": sha, "cost_usd": 0.25, "error": feedback,
                       "result": {**out["result"], "failure_kind": "author", "feedback": feedback}}
        _write_atomic(cache, json.dumps(out))
        _log(work, "generate", job["id"], "finish")
        return out

    return run


# --- worlds (agent mode) -----------------------------------------------------------


def world(backend: Backend):
    """run_world_job: build a world (origin initial), repair one, or mutate a parent.
    The world records its lineage, so tests can follow mutations across versions."""
    work = backend.work_dir

    def run(job: dict[str, Any]) -> dict[str, Any]:
        sc, inp = scenario_for(job["task_id"]), job["input"] or {}
        cache = work / "e2e" / "llm" / f"{job['id']}.json"
        if cache.exists():
            _log(work, "world", job["id"], "adopt")
            return {**json.loads(cache.read_text()), "cost_usd": 0.0}
        _log(work, "world", job["id"], "start")
        time.sleep(STEP_S)
        if parent := inp.get("parent_world_sha256"):
            assert (backend.artifacts.world_path(parent) / "world.json").is_file(), parent
        if sc.get("world") == "fail":
            out = {"outcome": "FAILED", "error": "the agent could not build a runnable world", "cost_usd": 0.3}
        else:
            doc = json.dumps({"job": job["id"], "origin": inp.get("origin", "initial"),
                              "spec": inp.get("spec") or {},
                              "lineage": {"parent_world_sha256": parent,
                                          "mutations": inp.get("mutations") or []}}, sort_keys=True)
            sha = hashlib.sha256(doc.encode()).hexdigest()
            _write_atomic(work / "worlds" / sha / "world.json", doc)
            backend.artifacts.register_world(sha)
            out = {"outcome": "OK", "cost_usd": 0.3,
                   "result": {"world_sha256": sha, "world_path": str(work / "worlds" / sha),
                              "name": f"world-{job['task_id'].rsplit('-', 1)[1]}"}}
        _write_atomic(cache, json.dumps(out))
        _log(work, "world", job["id"], "finish")
        return out

    return run


def world_check(backend: Backend):
    """run_world_check_job: the first `world_failures` rounds of a task's world fail."""
    work = backend.work_dir

    def run(job: dict[str, Any]) -> dict[str, Any]:
        _log(work, "world_check", job["id"], "start")
        time.sleep(STEP_S)
        inp = job["input"] or {}
        sha = inp["world_sha256"]
        assert (backend.artifacts.world_path(sha) / "world.json").is_file(), sha  # fetched from S3 if remote
        ok = int(inp.get("round", 0)) >= scenario_for(job["task_id"]).get("world_failures", 0)
        feedback = "" if ok else "repro/repro.sh exited 0 on the unfixed world: the counterexample doesn't reproduce"
        _log(work, "world_check", job["id"], "finish")
        return {"outcome": "OK" if ok else "FAILED", "error": feedback or None,
                "result": {"world_sha256": sha, "feedback": feedback,
                           "failed_check": None if ok else "repro_fails",
                           "checks": {"build": True, "repro_fails": ok, "repro_passes_with_fix": ok}}}

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


# --- GLM-5.1 failure review -----------------------------------------------------


def review(messages, response_format, llm_dir=None, cache_key="") -> SimpleNamespace:
    """The Trace Parser's review of one failed run: blames the agent, quoting the
    first evidence line (the failing test from the verifier output). Free."""
    first = next(line for line in messages[0]["content"].splitlines() if line.startswith("[E1]"))
    reply = {"explanation": first.split(") ", 1)[1], "attribution": "model", "pitfall_matched": "none",
             "cited_evidence": ["E1"]}
    return SimpleNamespace(content=json.dumps(reply), cost_usd=0.0, cached=False)


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


# --- the REAL agent adapters, with only the authoring trial and Docker faked ---------
#
# shared/jobs/worker.py's world_fn / draft_fn / world_check_fn run for real: they fetch
# worlds, bundles and draft records from the artifact store (S3 in cluster mode),
# register what the author wrote, save the authoring trial, and map results. Only
# what needs a model or Docker is replaced: author.run_world_job / run_create_job (at
# module level, where the adapters import them) and world_check._docker.


def _authoring_trial(work: Path, job_id: str) -> bool:
    """A finished trial dir like the harness leaves; True if one was already there (adopted)."""
    result = work / "traces" / job_id / "authoring" / "result.json"
    if result.exists():
        return True
    _write_atomic(result, json.dumps({"trial_name": job_id, "agent_result": {"cost_usd": 0.3}}))
    return False


def _store_tree(src: Path, dest: Path) -> None:
    if not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src, dest)


def _tree_sha(root: Path) -> str:
    h = hashlib.sha256()
    for f in sorted(p for p in root.rglob("*") if p.is_file()):
        h.update(str(f.relative_to(root)).encode() + b"\0" + f.read_bytes())
    return h.hexdigest()


def author_world_job(job_id: str, job_input: dict, work_dir=None, **_: Any) -> dict:
    from shared.taskformat import schema  # shared/taskformat/schema.py, on the path once the adapters import task-builder

    work = Path(work_dir)
    for key in ("parent_world_sha256", "world_sha256"):  # the adapter must have fetched it
        if sha := job_input.get(key):
            assert (work / "worlds" / sha / "world.json").is_file(), f"{key} {sha} not local"
    adopted = _authoring_trial(work, job_id)
    _log(work, "world", job_id, "adopt" if adopted else "start")
    with tempfile.TemporaryDirectory() as tmp:
        w = Path(tmp)
        _write_atomic(w / "environment" / "Dockerfile",
                      f"FROM {sorted(schema.BASE_IMAGES)[0]}\nWORKDIR /app\nCOPY app.py /app/app.py\n")
        _write_atomic(w / "environment" / "app.py", f"# world for {job_id}\n")
        _write_atomic(w / "repro" / "repro.sh", "exit 1\n")
        _write_atomic(w / "world_fix" / "fix.sh", "true\n")
        _write_atomic(w / "world.json", json.dumps({
            "invariants": ["confirmed reservations never exceed stock"],
            "counterexample": {"sequence": ["A reads stock=1", "B reads stock=1", "both confirm"],
                               "reproduce": "repro/repro.sh", "observed": "2 confirmed, stock 1"},
            "lineage": {"parent_world_sha256": job_input.get("parent_world_sha256"),
                        "mutations": job_input.get("mutations") or []}}, sort_keys=True))
        sha = _tree_sha(w)
        _store_tree(w, work / "worlds" / sha)
    _log(work, "world", job_id, "finish")
    return {"outcome": "OK", "error": None, "cost_usd": 0.0 if adopted else 0.3,
            "result": {"world_sha256": sha, "world_path": str(work / "worlds" / sha), "name": "world",
                       "authoring": {}}}


def author_create_job(job_id: str, job_input: dict, work_dir=None, **_: Any) -> dict:
    work = Path(work_dir)
    world = job_input["world_sha256"]
    assert (work / "worlds" / world / "world.json").is_file(), f"world {world} not local"
    if parent := job_input.get("bundle_sha256"):  # a revision or repair: its parent came along
        assert (work / "bundles" / parent / "instruction.md").is_file(), f"bundle {parent} not local"
        assert (work / "drafts" / f"{parent}.json").is_file(), f"draft {parent} not local"
    adopted = _authoring_trial(work, job_id)
    _log(work, "generate", job_id, "adopt" if adopted else "start")
    task_id, version = re.match(r"(.+)-v(\d+)-", job_id).groups()
    sc = scenario_for(task_id)
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        (d / "instruction.md").write_text(f"Task {task_id} v{version} from world {world[:12]} ({job_id})\n")
        (d / "scenario.json").write_text(json.dumps({"pattern": sc.get("versions", {}).get(version, "01000"),
                                                     "gate_failures": sc.get("gate_failures", 0)}))
        sha = _tree_sha(d)
        _store_tree(d, work / "bundles" / sha)
    _write_atomic(work / "drafts" / f"{sha}.json", json.dumps({"draft": {"name": f"agent-{task_id}"}}))
    _log(work, "generate", job_id, "finish")
    return {"outcome": "OK", "error": None, "cost_usd": 0.0 if adopted else 0.5,
            "result": {"name": f"agent-{task_id.rsplit('-', 1)[1]}", "bundle_sha256": sha,
                       "bundle_path": str(work / "bundles" / sha), "world_sha256": world,
                       "draft_path": str(work / "drafts" / f"{sha}.json"), "llm_calls": []}}


def docker(args: list[str], timeout: float) -> tuple[int, str]:
    """world_check's docker: builds succeed; the repro fails unfixed and passes fixed."""
    if args and args[0] == "run":
        return (0 if "fix.sh" in args[-1] else 1), ""
    return 0, ""
