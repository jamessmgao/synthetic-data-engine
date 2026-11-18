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
        <trial>/artifacts/              what the agent wrote to /logs/artifacts/

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
import subprocess
from datetime import datetime
from pathlib import Path

from shared.contracts import config
from harbor.models.task.config import TaskConfig

JOB_NAME = "authoring"
DEFAULT_IMAGE = "sde-authoring:dev"

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


def _task_toml(job_id: str, timeout_sec: float) -> str:
    return TaskConfig.model_validate({
        "task": {"name": f"{config.TASK_ORG}/authoring-{job_id}"[:120],
                 "description": f"Agent authoring run for pipeline job {job_id}"},
        "metadata": {"difficulty": "hard", "category": "authoring"},
        "verifier": {"timeout_sec": 300.0},
        "agent": {"timeout_sec": float(timeout_sec)},
        # The agent's worlds run in their own containers (through the socket), so
        # the authoring container itself needs little.
        "environment": {"build_timeout_sec": config.BUILD_TIMEOUT_SEC, "cpus": 2, "memory_mb": 4096,
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


def _finished_trial(job_dir: Path) -> tuple[Path | None, dict | None]:
    """The trial Harbor wrote under job_dir, if it got as far as a result.json."""
    trials = sorted(p.parent for p in job_dir.glob("*/result.json"))
    if not trials:
        return None, None
    return trials[0], json.loads((trials[0] / "result.json").read_text())


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
        "artifacts_dir": str(trial_dir / "artifacts"),
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
                        work_dir: str | Path | None = None, timeout_sec: float = 3600) -> dict:
    """Run (or adopt) the job's authoring trial; see the module docstring."""
    trace = Path(work_dir or config.WORK_DIR) / "traces" / job_id
    job_dir = trace / JOB_NAME
    trial_dir, result = _finished_trial(job_dir)
    if result is not None:
        return summarize(trial_dir, result, adopted=True)
    if job_dir.exists():  # a run that died before its result.json: keep it for the audit
        n = 1
        while job_dir.with_name(f"{JOB_NAME}.abandoned-{n}").exists():
            n += 1
        job_dir.rename(job_dir.with_name(f"{JOB_NAME}.abandoned-{n}"))

    task_dir = render_task(trace / "task", job_id, instruction, files, timeout_sec)
    try:
        subprocess.run(
            [config.HARBOR_BIN, "run", "-p", str(task_dir), "-a", "terminus-2",
             "-m", f"openrouter/{config.MODEL}", "-o", str(trace), "--job-name", JOB_NAME, "-y", "-q"],
            capture_output=True, text=True, env=dict(os.environ),
            timeout=timeout_sec + config.BUILD_TIMEOUT_SEC + 900,
        )
    except subprocess.TimeoutExpired:
        pass  # whatever Harbor wrote is read below; a missing result is reported as an error
    finally:
        cleanup_docker(job_id)
    trial_dir, result = _finished_trial(job_dir)
    return summarize(trial_dir, result, adopted=False)
