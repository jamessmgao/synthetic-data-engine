"""
One agent authoring run: a Harbor trial of terminus-2 + GLM-5.1 in the authoring
image (docs/AGENTIC_AUTHORING_PLAN.md §3.4). Shared by the world job
(author.run_world_job) and the create job (author_task.run_create_job).

    run_authoring_trial(job_id, instruction, files, work_dir) -> {
        trial_dir, artifacts_dir, cost_usd, gateway_cost_usd, steps, duration_sec,
        n_input_tokens, n_output_tokens, exception_type, error, adopted}

Layout, all under the job's trace dir (the pipeline saves it to S3 and fetches it
before a reclaimed job runs, so a finished trial is adopted, never paid twice):

    <work>/traces/<job_id>/task/        the rendered authoring task
    <work>/traces/<job_id>/authoring/   Harbor's job dir (-o traces/<job_id> --job-name authoring)
        <trial>/artifacts/logs/artifacts/   what the agent wrote to /logs/artifacts/
                                            (Harbor 0.26's layout, per artifacts/manifest.json)

The rendered task:
- environment/Dockerfile: FROM $SDE_AUTHORING_IMAGE, plus COPY of `files` to /input/;
- environment/docker-compose.yaml: mounts the host's Docker socket, so the agent can
  build and run worlds (no host bind mounts otherwise: see authoring/Dockerfile);
  SDE_DOCKER_LABEL=sde.job=<job_id> labels all of it, and it's removed after the trial;
- instruction.md: `instruction`; agent timeout `timeout_sec`;
- a trivial verifier (reward 1 if /logs/artifacts is non-empty): the trial's outputs
  are judged by the caller, not by Harbor.

Cost comes from the trial's token counts × config.GENERATOR_PRICE_*: the gateway's
usage.cost for glm-5.1 is a flat placeholder (5e-05), so LiteLLM's cost_usd is kept
only as gateway_cost_usd, for audit.
"""

import json
import os
import shutil
import signal
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

from shared.contracts import config
from harbor.models.task.config import TaskConfig

JOB_NAME = "authoring"
DEFAULT_IMAGE = "sde-authoring:dev"
# The authoring container itself; the worlds it builds run in their own containers,
# capped by sde-world / sde-task at the evaluation's 1 CPU / 2 GB.
AUTHORING_CPUS = int(os.environ.get("SDE_AUTHORING_CPUS", "1"))
AUTHORING_MEMORY_MB = int(os.environ.get("SDE_AUTHORING_MEMORY_MB", "2048"))

COMPOSE = """\
# The authoring agent builds and runs worlds through the host's Docker daemon.
services:
  main:
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
"""

# The trial's outputs are judged by the caller (world_check, TaskDraft validation),
# so the verifier only records whether the agent left any artifact.
TEST_SH = """\
#!/bin/bash
mkdir -p /logs/verifier
if [ -n "$(ls -A /logs/artifacts 2>/dev/null)" ]; then echo 1; else echo 0; fi > /logs/verifier/reward.txt
"""


def authoring_image() -> str:
    return os.environ.get("SDE_AUTHORING_IMAGE") or DEFAULT_IMAGE


def docker_label(job_id: str) -> str:
    return f"sde.job={job_id}"


def cleanup_docker(job_id: str) -> None:
    """Remove the containers and images the agent left behind through the socket
    (servers left running, a run cut off by the timeout, every world build). They'd
    otherwise hold CPU, RAM and disk on the runner, uncounted by its Docker slots."""
    label = f"label={docker_label(job_id)}"
    ps = subprocess.run(["docker", "ps", "-sde", "--filter", label], capture_output=True, text=True)
    ids = ps.stdout.split()
    if ids:
        subprocess.run(["docker", "rm", "-f", *ids], capture_output=True, text=True)
    subprocess.run(["docker", "image", "prune", "-af", "--filter", label], capture_output=True, text=True)


def remove_trial_networks(job_dir: Path) -> list[str]:
    """Remove the compose network of every trial under `job_dir`. Harbor names a trial's
    compose project `<trial dir name, lowercased>__env`, and its network
    `<project>_default`. A Harbor that was killed or crashed never runs compose down,
    and the leaked networks exhausted Docker's address pools (local-agent-b2: "all
    predefined address pools have been fully subnetted"). `docker network rm` refuses a
    network still in use, so a live trial's network is left alone. Returns the
    networks it asked to remove."""
    if not job_dir.is_dir():
        return []
    names = [f"{trial.name.lower()}__env_default" for trial in sorted(job_dir.iterdir()) if trial.is_dir()]
    for name in names:
        subprocess.run(["docker", "network", "rm", name], capture_output=True, text=True)
    return names


def _task_toml(job_id: str, timeout_sec: float) -> str:
    return TaskConfig.model_validate({
        "task": {"name": f"{config.TASK_ORG}/authoring-{job_id}"[:120],
                 "description": f"Agent authoring run for pipeline job {job_id}"},
        "metadata": {"difficulty": "hard", "category": "authoring"},
        "verifier": {"timeout_sec": 300.0},
        "agent": {"timeout_sec": float(timeout_sec)},
        # The agent's worlds run in their own containers (through the socket), so
        # the authoring container itself needs little.
        "environment": {"build_timeout_sec": config.BUILD_TIMEOUT_SEC, "cpus": AUTHORING_CPUS,
                        "memory_mb": AUTHORING_MEMORY_MB,
                        # sde-world / sde-task label everything they start with it; so must the agent.
                        "env": {"SDE_DOCKER_LABEL": docker_label(job_id)}},
    }).model_dump_toml()


def render_task(task_dir: Path, job_id: str, instruction: str, files: dict[str, bytes],
                timeout_sec: float) -> Path:
    """Write the authoring task (replacing any previous rendering of it)."""
    if task_dir.exists():
        shutil.rmtree(task_dir)
    env = task_dir / "environment"
    (env / "input").mkdir(parents=True)
    for rel, data in files.items():
        target = (env / "input" / rel).resolve()
        if not target.is_relative_to((env / "input").resolve()):
            raise ValueError(f"input path escapes /input: {rel}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    (env / "Dockerfile").write_text(f"FROM {authoring_image()}\nCOPY input/ /input/\nWORKDIR /work\n")
    (env / "docker-compose.yaml").write_text(COMPOSE)
    (task_dir / "instruction.md").write_text(instruction if instruction.endswith("\n") else instruction + "\n")
    (task_dir / "task.toml").write_text(_task_toml(job_id, timeout_sec))
    (task_dir / "solution").mkdir()
    (task_dir / "solution" / "solve.sh").write_text("#!/bin/bash\ntrue\n")
    (task_dir / "tests").mkdir()
    (task_dir / "tests" / "test.sh").write_text(TEST_SH)
    for script in (task_dir / "solution" / "solve.sh", task_dir / "tests" / "test.sh"):
        script.chmod(0o755)
    return task_dir


def _usable(trial_dir: Path, result: dict) -> bool:
    """Whether a finished trial is worth adopting. Harbor writes result.json even when the
    trial died of an infra error (containerd "failed to export layer", a compose service
    that never resolved): adopting that one failed the job the same way on every retry,
    re-downloaded from S3 each time. A trial with an exception counts only if it still
    produced artifacts (e.g. the agent ran out of time after writing its world)."""
    if not result.get("exception_info"):
        return True
    out = artifacts_dir(trial_dir)
    return out.is_dir() and any(out.iterdir())


def _finished_trial(job_dir: Path) -> tuple[Path | None, dict | None]:
    """A usable trial Harbor wrote under job_dir (see `_usable`), if any. Several can come
    back from S3 after retries; prefer one that worked."""
    for trial in sorted(p.parent for p in job_dir.glob("*/result.json")):
        result = json.loads((trial / "result.json").read_text())
        if _usable(trial, result):
            return trial, result
    return None, None


def artifacts_dir(trial_dir: Path) -> Path:
    """Where the trial's copy of the container's /logs/artifacts/ is.

    Harbor 0.26 mirrors it to <trial>/artifacts/logs/artifacts/ and records the
    destination (relative to the trial dir) in <trial>/artifacts/manifest.json.
    Read the manifest; fall back to the known layouts if it's missing."""
    root = trial_dir / "artifacts"
    try:
        entries = json.loads((root / "manifest.json").read_text())
    except (OSError, json.JSONDecodeError):
        entries = []
    for e in entries if isinstance(entries, list) else []:
        if (isinstance(e, dict) and str(e.get("source", "")).rstrip("/") == "/logs/artifacts"
                and e.get("destination") and (trial_dir / e["destination"]).is_dir()):
            return trial_dir / e["destination"]
    for candidate in (root / "logs" / "artifacts", root):
        if candidate.is_dir():
            return candidate
    return root


def _seconds(start: str | None, end: str | None) -> float | None:
    try:
        return (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds()
    except (TypeError, ValueError):
        return None


def summarize(trial_dir: Path | None, result: dict | None, adopted: bool) -> dict:
    """The harness result for a trial (or for no trial at all)."""
    if result is None:
        return {"trial_dir": None, "artifacts_dir": None, "cost_usd": 0.0, "gateway_cost_usd": None,
                "steps": 0, "duration_sec": None, "n_input_tokens": 0, "n_output_tokens": 0,
                "exception_type": "NoTrialResult", "error": "Harbor wrote no trial result", "adopted": adopted}
    agent = result.get("agent_result") or {}
    n_in, n_out = int(agent.get("n_input_tokens") or 0), int(agent.get("n_output_tokens") or 0)
    exc = result.get("exception_info") or {}
    execution = result.get("agent_execution") or {}
    return {
        "trial_dir": str(trial_dir),
        "artifacts_dir": str(artifacts_dir(trial_dir)),
        "cost_usd": round(n_in / 1e6 * config.GENERATOR_PRICE_IN_PER_M
                          + n_out / 1e6 * config.GENERATOR_PRICE_OUT_PER_M, 6),
        "gateway_cost_usd": agent.get("cost_usd"),
        "steps": int((agent.get("metadata") or {}).get("n_episodes") or 0),
        "duration_sec": _seconds(execution.get("started_at"), execution.get("finished_at")),
        "n_input_tokens": n_in,
        "n_output_tokens": n_out,
        "exception_type": exc.get("exception_type"),
        "error": (f"{exc.get('exception_type')}: {(exc.get('exception_message') or '')[-1500:]}"
                  if exc else None),
        "adopted": adopted,
    }


def run_authoring_trial(job_id: str, instruction: str, files: dict[str, bytes],
                        work_dir: str | Path | None = None, timeout_sec: float = 900) -> dict:
    """Run (or adopt) the job's authoring trial; see the module docstring."""
    size = instruction_size(instruction)
    trace = Path(work_dir or config.WORK_DIR) / "traces" / job_id
    job_dir = trace / JOB_NAME
    trial_dir, result = _finished_trial(job_dir)
    if result is not None:
        return {**summarize(trial_dir, result, adopted=True), "instruction_size": size}
    if job_dir.exists():
        # Keep it for the audit: a run that died before its result.json is "abandoned", one
        # that finished with an infra error and nothing to show is "infra-failed".
        kind = "infra-failed" if any(job_dir.glob("*/result.json")) else "abandoned"
        n = 1
        while job_dir.with_name(f"{JOB_NAME}.{kind}-{n}").exists():
            n += 1
        job_dir.rename(job_dir.with_name(f"{JOB_NAME}.{kind}-{n}"))

    task_dir = render_task(trace / "task", job_id, instruction, files, timeout_sec)
    stop = _RUNNING.setdefault(job_id, threading.Event())
    stopped = False
    try:
        proc = subprocess.Popen(
            harbor_command(task_dir, trace), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL, env=dict(os.environ),
            start_new_session=True,  # its own process group: stop Harbor and its children together
        )
        deadline = time.monotonic() + timeout_sec + config.BUILD_TIMEOUT_SEC + 900
        while proc.poll() is None:
            if stop.wait(POLL_SEC):
                stopped = True  # the job was taken away (its task cancelled): stop the trial now
                _stop(proc)
            elif time.monotonic() > deadline:
                _stop(proc)  # whatever Harbor wrote is read below; a missing result is an error
    finally:
        _RUNNING.pop(job_id, None)
        cleanup_docker(job_id)
        remove_trial_networks(job_dir)
    trial_dir, result = _finished_trial(job_dir)
    out = {**summarize(trial_dir, result, adopted=False), "instruction_size": size}
    if stopped and result is None:
        out.update(exception_type="Cancelled", error="the authoring trial was stopped: its job was taken away")
    return out


POLL_SEC = 2.0
STOP_GRACE_SEC = 30.0
# Authoring trials running in this process, by job id: stop_trial(job_id) stops one.
_RUNNING: dict[str, threading.Event] = {}


def harbor_command(task_dir: Path, trace: Path) -> list[str]:
    cmd = [config.HARBOR_BIN, "run", "-p", str(task_dir), "-a", "terminus-2",
           "-m", f"openrouter/{config.MODEL}", "-o", str(trace), "--job-name", JOB_NAME, "-y", "-q"]
    # SDE_AUTHORING_REASONING_EFFORT (e.g. medium): the authoring agent's reasoning effort;
    # unset = the provider's default.
    if effort := os.environ.get("SDE_AUTHORING_REASONING_EFFORT", "").strip():
        cmd += ["--ak", f"reasoning_effort={effort}"]
    if llm := llm_kwargs():
        cmd += ["--ak", f"llm_kwargs={json.dumps(llm, sort_keys=True)}"]
    return cmd


def llm_kwargs() -> dict:
    """terminus-2's `llm_kwargs`, which Harbor's LiteLLM spreads into every model call:
    SDE_AUTHORING_LLM_TIMEOUT_S caps one call (litellm's default is 6000 s; in round 1 a
    hung call cost ~600 s before Harbor's retry), SDE_AUTHORING_MAX_TOKENS the output of
    one (litellm has no model info for glm-5.1, so the provider's default applies). Unset
    = not sent. Invalid values raise, so a typo doesn't silently drop the cap."""
    out: dict = {}
    if raw := os.environ.get("SDE_AUTHORING_LLM_TIMEOUT_S", "").strip():
        if (timeout := float(raw)) <= 0:
            raise ValueError("SDE_AUTHORING_LLM_TIMEOUT_S must be > 0")
        out["timeout"] = timeout
    if raw := os.environ.get("SDE_AUTHORING_MAX_TOKENS", "").strip():
        if (tokens := int(raw)) <= 0:
            raise ValueError("SDE_AUTHORING_MAX_TOKENS must be > 0")
        out["max_tokens"] = tokens
    return out


def stop_trial(job_id: str) -> bool:
    """Stop this process's running authoring trial for `job_id` (its lease was lost: the
    task was cancelled, or another worker took the job). True if one was running."""
    event = _RUNNING.get(job_id)
    if event is None:
        return False
    event.set()
    return True


def _stop(proc: subprocess.Popen) -> None:
    """SIGTERM Harbor's process group so it can remove its containers; SIGKILL after the
    grace period (as evaluation/harbor.py does for rollouts)."""
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):  # already gone (macOS: an unreaped zombie)
        proc.wait()
        return
    try:
        proc.wait(timeout=STOP_GRACE_SEC)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        proc.wait()


def instruction_size(instruction: str) -> dict:
    """The rendered instruction's size: the context every agent step starts from.
    `tokens_est` is characters / 4 (a stdlib estimate, near cl100k on this English and
    code mix; the worker images have no tokenizer)."""
    return {"chars": len(instruction), "tokens_est": round(len(instruction) / 4)}
