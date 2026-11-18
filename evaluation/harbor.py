"""
Run exactly one Harbor trial for one rollout job, and find it again after a crash.

Each job gets its own Harbor job directory, ``<traces_dir>/<job_id>/``, used
as both ``--job-name`` and output folder. That directory is the raw trace the
PRD keeps in S3, and it is also how a restarted executor avoids paying for a
run twice:

1. **Finished trial on disk** (a trial ``result.json`` with ``finished_at``):
   adopt it. The previous worker died after Harbor finished but before the
   result was recorded.
2. **Harbor still running** (``<job_id>.pid`` names a live process on this
   host): wait for it and adopt its result. The previous worker was killed
   but its Harbor child kept going.
3. **Half-finished directory**, or a trial Harbor recorded as cancelled
   because we stopped it at shutdown: keep it as ``<job_id>.abandoned-<n>``
   for the audit trail and start a fresh run.

Harbor runs in its own process group so that cancelling a run (shutdown, or a
lost lease) stops Harbor and its Docker containers together.
"""

import json
import os
import signal
import socket
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path

from evaluation import settings


# What Harbor records for a trial it was told to stop (SIGTERM).
CANCELLED = "CancelledError"


class HarborError(Exception):
    """Harbor exited without leaving a finished trial behind."""


class Cancelled(Exception):
    """The run was stopped on purpose (shutdown or lost lease)."""


@dataclass(frozen=True)
class AgentConfig:
    agent: str
    model: str
    kwargs: dict

    def as_expected(self) -> dict:
        """The shape ``classify`` compares against Harbor's recorded trial config."""
        return {"agent": self.agent, "model": self.model, "kwargs": self.kwargs}


@dataclass(frozen=True)
class TrialRun:
    result: dict  # the trial's result.json
    trial_dir: Path
    adopted: bool  # True if this run was found on disk rather than started now


def pinned_agent() -> AgentConfig:
    return AgentConfig(
        agent=settings.EVAL_AGENT,
        model=settings.EVAL_MODEL,
        kwargs={"reasoning_effort": settings.EVAL_REASONING_EFFORT},
    )


def command(task_dir: Path, job_id: str, agent: AgentConfig, traces_dir: Path,
            env_file: Path | None) -> list[str]:
    cmd = [
        settings.HARBOR_BIN, "run",
        "-p", str(task_dir),
        "-a", agent.agent,
        "--n-attempts", "1",
        "--n-concurrent", "1",
        "--max-retries", "0",  # retrying is the executor's job, as a new job row
        "--job-name", job_id,
        "-o", str(traces_dir),
        "--quiet", "--yes",
    ]
    if agent.model:
        cmd += ["-m", agent.model]
    for key, value in sorted(agent.kwargs.items()):
        cmd += ["--ak", f"{key}={value}"]
    if env_file is not None and env_file.exists():
        cmd += ["--env-file", str(env_file)]
    return cmd


def finished_trial(job_dir: Path) -> TrialRun | None:
    """The finished trial inside a Harbor job directory, if there is one."""
    if not job_dir.is_dir():
        return None
    for result_path in sorted(job_dir.glob("*/result.json")):
        try:
            result = json.loads(result_path.read_text())
        except (OSError, json.JSONDecodeError):
            continue  # Harbor was killed mid-write
        if (result.get("exception_info") or {}).get("exception_type") == CANCELLED:
            continue  # stopped on purpose (shutdown): not a measurement, rerun it
        if result.get("trial_name") and result.get("finished_at"):
            return TrialRun(result=result, trial_dir=result_path.parent, adopted=True)
    return None


def _pid_file(traces_dir: Path, job_id: str) -> Path:
    return traces_dir / f"{job_id}.pid"


def _is_running(pid: int, job_id: str) -> bool:
    """True if ``pid`` is alive and is the Harbor run for ``job_id`` (not a reused pid)."""
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return job_id in _command_line(pid)


def _command_line(pid: int) -> str:
    """The process's command line. /proc on Linux (the runner image has no `ps`: on AWS
    the old call raised FileNotFoundError and a reclaimed rollout came out INVALID);
    `ps` elsewhere (macOS); "" if neither can tell."""
    proc = Path(f"/proc/{pid}/cmdline")
    if proc.parent.is_dir():
        try:
            return proc.read_bytes().replace(b"\0", b" ").decode(errors="replace")
        except OSError:
            return ""
    try:
        return subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True).stdout
    except OSError:
        return ""


def _live_pid(pid_file: Path, job_id: str) -> int | None:
    """The pid recorded for this job's still-running Harbor process on this host, if any."""
    try:
        info = json.loads(pid_file.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    pid = info.get("pid")
    if info.get("host") != socket.gethostname() or not isinstance(pid, int):
        return None
    return pid if _is_running(pid, job_id) else None


def _set_aside(job_dir: Path) -> None:
    n = 1
    while (aside := job_dir.with_name(f"{job_dir.name}.abandoned-{n}")).exists():
        n += 1
    job_dir.rename(aside)


def _stop(proc: subprocess.Popen, grace_sec: float) -> None:
    """SIGTERM Harbor's process group so it can remove its containers; SIGKILL after the grace period."""
    # On macOS, signalling a group that only holds an unreaped zombie raises
    # PermissionError rather than ProcessLookupError; both mean "already gone".
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        proc.wait()
        return
    try:
        proc.wait(timeout=grace_sec)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        proc.wait()


def _wait_for_orphan(pid: int, job_id: str, cancel: threading.Event, poll_sec: float) -> None:
    """Wait for a Harbor process a dead worker left running."""
    while _is_running(pid, job_id):
        if cancel.wait(poll_sec):
            # Not ours to kill: if we are shutting down, the next owner adopts it.
            raise Cancelled()


def _tail(path: Path, n: int = 20) -> str:
    try:
        return "\n".join(path.read_text(errors="replace").splitlines()[-n:])
    except OSError:
        return ""


def run_trial(task_dir: Path, job_id: str, agent: AgentConfig, cancel: threading.Event,
              traces_dir: Path, env_file: Path | None = None,
              poll_sec: float = 1.0) -> TrialRun:
    """Run (or recover) the single Harbor trial for ``job_id``.

    Raises ``Cancelled`` if ``cancel`` is set while Harbor is running, and
    ``HarborError`` if Harbor exits without a finished trial.
    """
    env_file = env_file if env_file is not None else settings.ENV_FILE
    traces_dir.mkdir(parents=True, exist_ok=True)
    job_dir = traces_dir / job_id
    pid_file = _pid_file(traces_dir, job_id)

    if (orphan := _live_pid(pid_file, job_id)) is not None:
        _wait_for_orphan(orphan, job_id, cancel, poll_sec)
    if (found := finished_trial(job_dir)) is not None:
        pid_file.unlink(missing_ok=True)
        return found
    if cancel.is_set():
        raise Cancelled()
    if job_dir.exists():
        _set_aside(job_dir)

    log_path = traces_dir / f"{job_id}.log"
    with open(log_path, "ab") as log:
        proc = subprocess.Popen(
            command(task_dir, job_id, agent, traces_dir, env_file),
            stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            start_new_session=True,  # own process group: stop Harbor and its children together
        )
    pid_file.write_text(json.dumps({"pid": proc.pid, "host": socket.gethostname(), "job_id": job_id}))
    try:
        while proc.poll() is None:
            if cancel.wait(poll_sec):
                _stop(proc, settings.CANCEL_GRACE_SEC)
                raise Cancelled()
    finally:
        if proc.poll() is not None:
            pid_file.unlink(missing_ok=True)

    found = finished_trial(job_dir)
    if found is None:
        raise HarborError(f"harbor exited {proc.returncode} without a finished trial; "
                          f"log tail:\n{_tail(log_path)}")
    return TrialRun(result=found.result, trial_dir=found.trial_dir, adopted=False)
