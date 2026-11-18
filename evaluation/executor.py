"""
Executor workers: claim ``rollout`` jobs, run one Harbor trial each, report the result.

This is the "Executor workers" box in docs/PRD.md §5. An executor never
decides whether a task is accepted; it turns one job into one measurement and
settles it. The Analysis service reads the five valid measurements and their
traces, explains the failures (evaluation/trace_parser.py) and decides.

For each claimed job:

1. a heartbeat thread keeps the lease alive; if the lease is lost (another
   worker took the job over), Harbor is stopped and nothing is recorded;
2. ``harbor.run_trial`` runs, or recovers after a crash, the job's single
   trial, against the immutable bundle named by the job's ``bundle_sha256``;
3. the trace is uploaded to the artifact store at once, so if this worker dies
   another machine adopts the finished trial instead of rerunning it;
4. ``classify`` turns its ``result.json`` into VALID_PASS / VALID_FAIL / INVALID;
5. the job is settled with outcome, reward, cost and trace location. An
   INVALID rollout (timeout, infra or API error) also enqueues a retry of
   the same slot in the same transaction, with backoff, up to
   MAX_ROLLOUT_RETRIES; it is never scored as a failure. Every settle also
   enqueues `<id>-analyze` on the analyze queue: the Trace Parser's trigger.

Queue, leases and fencing come from ``shared.jobs.jobqueue.JobQueue``; bundles and
traces from the backend's artifact store. A job whose workers keep dying is
not claimed again past MAX_JOB_ATTEMPTS and is dead-lettered by the reconciler.

Rollout job contract (what the validate worker enqueues, see ``rollout_job``):

    id             shared.storage.sql.rollout_job_id(task_id, version, slot, retry)
    type           "rollout"
    task_id, version, slot (1..N_RUNS), retry
    bundle_sha256  the version's bundle in the artifact store

Run the workers with ``python -m evaluation.executor``.
"""

import argparse
import logging
import os
import signal
import socket
import threading
import time
from collections import deque
from hashlib import sha256
from pathlib import Path
from typing import Callable

from evaluation import harbor, settings
from evaluation.classify import INVALID, classify
from shared.storage import Backend, describe, from_env
from shared.storage.sql import rollout_job_id
from shared.jobs.jobqueue import JobQueue
from shared.contracts.models import DOCKER_SLOTS, HEARTBEAT_S, LEASE_TTL_S, MAX_ROLLOUT_RETRIES, POLL_S, JobType

log = logging.getLogger("executor")


def rollout_job(task_id: str, version: int, slot: int, bundle_sha256: str, retry: int = 0,
                delay_sec: float = 0.0) -> dict:
    """A rollout job, as the Job Service (or a retry) enqueues it."""
    return {
        "id": rollout_job_id(task_id, version, slot, retry),
        "type": JobType.ROLLOUT,
        "task_id": task_id,
        "version": version,
        "slot": slot,
        "retry": retry,
        "bundle_sha256": bundle_sha256,
        "delay_sec": delay_sec,
    }


def analyze_job(rollout: dict) -> dict:
    """The ping a finished run sends the Trace Parser (one per run, never deduplicated
    across runs, so a run that settles after an earlier analysis still triggers one)."""
    return {
        "id": f"{rollout['id']}-analyze",
        "type": JobType.ANALYZE,
        "task_id": rollout["task_id"],
        "version": rollout["version"],
        "input": {"trigger": rollout["id"]},
    }


def config_hash(agent: harbor.AgentConfig) -> str:
    """Identifies the pinned evaluation config; five measurements must share one."""
    text = f"{agent.agent}|{agent.model}|{sorted(agent.kwargs.items())}"
    return sha256(text.encode()).hexdigest()


class _Breaker:
    """Pause claiming when many rollouts come back INVALID at once (gateway or Docker down)."""

    def __init__(self, threshold: int, window_sec: float, pause_sec: float):
        self.threshold, self.window_sec, self.pause_sec = threshold, window_sec, pause_sec
        self._invalid: deque[float] = deque()
        self._paused_until = 0.0
        self._lock = threading.Lock()

    def record_invalid(self) -> None:
        now = time.monotonic()
        with self._lock:
            self._invalid.append(now)
            while self._invalid and self._invalid[0] < now - self.window_sec:
                self._invalid.popleft()
            if len(self._invalid) >= self.threshold:
                self._paused_until = now + self.pause_sec
                self._invalid.clear()
                log.warning("%d invalid rollouts within %.0fs; pausing claims for %.0fs",
                            self.threshold, self.window_sec, self.pause_sec)

    def open_for(self) -> float:
        """Seconds left in the current pause (0 when claiming is allowed)."""
        with self._lock:
            return max(0.0, self._paused_until - time.monotonic())


RunTrial = Callable[..., harbor.TrialRun]


class Executor:
    def __init__(self, backend: Backend, run_trial: RunTrial = harbor.run_trial,
                 agent: harbor.AgentConfig | None = None, docker_slots: int = DOCKER_SLOTS,
                 lease_sec: float = LEASE_TTL_S, heartbeat_sec: float = HEARTBEAT_S,
                 poll_sec: float = POLL_S):
        self.backend = backend
        self.queue = JobQueue(backend)
        self.artifacts = backend.artifacts
        self.traces_dir = backend.artifacts.trace_dir("_").parent
        self.run_trial = run_trial
        self.agent = agent or harbor.pinned_agent()
        self.docker_slots = docker_slots
        self.lease_sec = lease_sec
        self.heartbeat_sec = heartbeat_sec
        self.poll_sec = poll_sec
        self.stop = threading.Event()
        self._running: set[threading.Event] = set()  # cancel events of trials in flight
        self._running_lock = threading.Lock()
        self.breaker = _Breaker(settings.BREAKER_INVALID, settings.BREAKER_WINDOW_SEC,
                                settings.BREAKER_PAUSE_SEC)

    # --- one job ------------------------------------------------------------------

    def run_one(self, owner: str) -> bool:
        """Claim and process one rollout job. Returns False if there was nothing to claim."""
        job = self.queue.claim(JobType.ROLLOUT, owner, self.lease_sec, self.docker_slots)
        if job is None:
            return False
        self.process(job)
        return True

    def process(self, job: dict) -> None:
        job_id, token = job["id"], job["lease_token"]
        cancel = threading.Event()
        lease_lost = threading.Event()
        with self._running_lock:
            self._running.add(cancel)
            if self.stop.is_set():
                cancel.set()
        beat = threading.Thread(target=self._heartbeat, args=(job_id, token, cancel, lease_lost),
                                name=f"heartbeat-{job_id}", daemon=True)
        beat.start()
        try:
            fields, followups = self._measure(job, cancel)
        except harbor.Cancelled:
            if lease_lost.is_set():
                log.warning("%s: lease lost to another worker; dropped without recording", job_id)
            else:
                self.queue.release(job_id, token)
                log.info("%s: stopped for shutdown; released back to the queue", job_id)
            return
        finally:
            with self._running_lock:
                self._running.discard(cancel)
            cancel.set()
            beat.join()

        followups = (*followups, analyze_job(job))
        if lease_lost.is_set() or not self.queue.settle(job_id, token, fields, followups):
            log.warning("%s: lease lost before settling; result discarded (the new owner will adopt it)", job_id)
            return
        if fields["outcome"] == INVALID:
            self.breaker.record_invalid()
        log.info("%s: %s%s", job_id, fields["outcome"],
                 f" ({fields['error']})" if fields.get("error") else f" reward={fields['reward']}")

    def _heartbeat(self, job_id: str, token: int, cancel: threading.Event, lease_lost: threading.Event) -> None:
        while not cancel.wait(self.heartbeat_sec):
            if not self.queue.heartbeat(job_id, token, self.lease_sec):
                lease_lost.set()
                cancel.set()
                return

    def _measure(self, job: dict, cancel: threading.Event) -> tuple[dict, tuple[dict, ...]]:
        """Run the trial and build the settle payload. Raises harbor.Cancelled.

        ``config_hash`` is left as the Job Service set it on the row; ``classify``
        checks the agent config Harbor actually ran against the pinned one.
        """
        fields: dict = {}
        job_id = job["id"]
        try:
            if job.get("bundle_sha256"):
                task_dir = self.artifacts.bundle_path(job["bundle_sha256"])  # downloads in S3 mode
            else:
                task_dir = Path(job["input"]["task_dir"])
            # A worker on another machine may have finished this trial and uploaded
            # it before dying: bring it here so run_trial adopts it. If nothing
            # complete exists, move any half-uploaded remote files aside first.
            if not self.artifacts.fetch_trace(job_id):
                self.artifacts.abandon_trace(job_id)
            run = self.run_trial(task_dir, job_id, self.agent, cancel, traces_dir=self.traces_dir)
            # Upload before anything slower (classification, the failure review),
            # so a crash from here on loses nothing another machine can't adopt.
            trace_uri = self.artifacts.save_trace(job_id)
        except harbor.Cancelled:
            raise
        except Exception as e:  # Harbor crashed or the bundle is missing: not the model's fault
            log.exception("%s: trial did not complete", job_id)
            fields.update(outcome=INVALID, reward=None, error=f"{type(e).__name__}: {str(e)[:500]}",
                          result={"adopted": False})
            return fields, self._retry(job)

        m = classify(run.result, self.agent.as_expected())
        fields.update(
            outcome=m.outcome,
            reward=m.reward,
            error=m.error,
            cost_usd=m.cost_usd,
            trace_uri=trace_uri,
            result={
                "adopted": run.adopted,
                "trial": run.trial_dir.name,
                "raw_reward": m.raw_reward,
                "task_checksum": m.task_checksum,
                "n_input_tokens": m.n_input_tokens,
                "n_output_tokens": m.n_output_tokens,
                "duration_sec": m.duration_sec,
            },
        )
        return fields, self._retry(job) if m.outcome == INVALID else ()

    def _retry(self, job: dict) -> tuple[dict, ...]:
        retry = job["retry"] + 1
        if retry > MAX_ROLLOUT_RETRIES:
            log.error("%s: invalid after %d retries; slot left unresolved", job["id"], job["retry"])
            return ()
        delay = settings.RETRY_BACKOFF_SEC * 2 ** job["retry"]
        return (rollout_job(job["task_id"], job["version"], job["slot"], job["bundle_sha256"],
                            retry=retry, delay_sec=delay),)

    # --- worker loop ----------------------------------------------------------------

    def _worker(self, owner: str, idle: Callable[[], bool] | None) -> None:
        while not self.stop.is_set():
            pause = self.breaker.open_for()
            if pause:
                self.stop.wait(min(pause, self.poll_sec * 10))
                continue
            if self.run_one(owner):
                continue
            if idle is not None and idle():
                return
            self.stop.wait(self.poll_sec)

    def serve(self, n_workers: int | None = None, idle: Callable[[], bool] | None = None) -> None:
        """Run worker threads until ``stop`` is set, or until ``idle()`` says nothing is left."""
        n_workers = n_workers or self.docker_slots
        base = f"{socket.gethostname()}:{os.getpid()}"
        threads = [threading.Thread(target=self._worker, args=(f"{base}:{i}", idle), name=f"executor-{i}")
                   for i in range(n_workers)]
        # Ctrl-C and SIGTERM only request a stop. Letting KeyboardInterrupt
        # interrupt Thread.join() can leave the thread looking finished while
        # it is still stopping Harbor, so the process would exit mid-cleanup.
        previous = {}
        if threading.current_thread() is threading.main_thread():
            for sig in (signal.SIGINT, signal.SIGTERM):
                previous[sig] = signal.signal(sig, lambda *_: self.shutdown())
        try:
            for t in threads:
                t.start()
            for t in threads:
                while t.is_alive():
                    t.join(timeout=0.5)
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)

    def shutdown(self) -> None:
        """Stop claiming, cancel every running trial and release its job."""
        if not self.stop.is_set():
            log.info("stopping: cancelling running trials and releasing their jobs")
        self.stop.set()
        with self._running_lock:
            for cancel in self._running:
                cancel.set()


def no_open_rollouts(backend: Backend, batch_id: str | None = None) -> Callable[[], bool]:
    """``idle`` callback: true once no rollout job is READY or LEASED."""
    def idle() -> bool:
        return backend.store.conn.execute(
            "SELECT COUNT(*) AS n FROM jobs j JOIN tasks t ON t.id = j.task_id"
            " WHERE j.type = 'rollout' AND j.status IN ('READY', 'LEASED')"
            " AND (? IS NULL OR t.batch_id = ?)",
            (batch_id, batch_id),
        ).fetchone()["n"] == 0
    return idle


def main() -> None:
    parser = argparse.ArgumentParser(description="Run executor workers against the pipeline's job queue.")
    parser.add_argument("--workers", type=int, default=DOCKER_SLOTS, help="concurrent rollouts in this process")
    parser.add_argument("--until-idle", action="store_true", help="exit once no rollout jobs are left")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(threadName)s %(message)s")

    backend = from_env()
    log.info("executor: %d workers, backends %s, agent %s", args.workers, describe(backend), harbor.pinned_agent())
    Executor(backend).serve(n_workers=args.workers, idle=no_open_rollouts(backend) if args.until_idle else None)


if __name__ == "__main__":
    main()
