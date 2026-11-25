"""The generic service loop: what the Generation Service (generator/service.py), Build +
Validate (validator/service.py), the Executor and the Trace Parser run their jobs on.

`serve()` is the loop: claim a job, mark the task's stage (`on_start`), keep the lease
alive with a heartbeat thread, run a job function, settle the result together with the
hand-off to the next stage (`handoff`, run inside the settle transaction, see
shared/jobs/handoffs.py). If the lease is lost mid-run, the result is dropped: the settle
is fenced, so it changes nothing. If the function raises, the job is released with a
backoff, and the reconciler dead-letters it after MAX_JOB_ATTEMPTS claims.

`run_service()` is the command line the two services share: the backends, the shutdown
handler (drain, Spot interruption), the optional reconciler thread, then the service's
own claim loops.
"""

import argparse
import logging
import os
import signal
import socket
import threading
import time
from collections.abc import Callable
from typing import Any

from shared.contracts.models import HEARTBEAT_S, LEASE_TTL_S, POLL_S, JobType
from shared.jobs import reconciler
from shared.jobs.jobqueue import Handoff, JobQueue
from shared.storage import Backend, dbretry, describe, from_env

log = logging.getLogger("worker")

JobFn = Callable[[dict[str, Any]], dict[str, Any]]
OnStart = Callable[[dict[str, Any]], None]
FAILURE_BACKOFF_S = 60.0
HEARTBEAT_RETRY_S = 5.0  # after a failed heartbeat, retry sooner than the normal interval
LOOP_ERROR_WAIT_S = 5.0  # after an unexpected error in a claim loop
STARTUP_WAIT_S = 600.0  # how long a starting worker waits for the database
# On SIGTERM, how long running jobs may go on before they're handed back (0: at once). Set
# on generation (aws-prod), with terminationGracePeriodSeconds above it, so a deploy lets an
# hour-long authoring session finish instead of re-paying it from the start.
DRAIN_ON_TERM_S = float(os.environ.get("SDE_DRAIN_ON_TERM_S", "0"))
# Jobs this process is running: job id -> (queue, lease token, job row, on_lost). A
# shutdown hands them straight back (release_in_flight) instead of leaving their leases to
# lapse: a stopped or killed worker otherwise stranded an agent job for its whole lease
# (SDE_AGENT_LEASE_TTL_S, 30 min) plus the suspect wait, and every forced restart cost the
# job one of its MAX_JOB_ATTEMPTS (round 2: a restart and a kill-all).
_in_flight: dict[str, tuple[JobQueue, int, dict[str, Any], "OnStart | None"]] = {}
_in_flight_lock = threading.Lock()


def release_in_flight(reason: str = "released at worker shutdown") -> list[str]:
    """Give every job this process is running back to the queue at once (READY, no
    backoff), then stop its work (`on_lost`, e.g. the authoring trial). Their late settles
    are refused by the fence (the job is no longer LEASED to this token), and the next
    owner adopts a finished trial instead of paying again. Returns the released job ids."""
    with _in_flight_lock:
        jobs = list(_in_flight.items())
    released = []
    for job_id, (jq, token, job, on_lost) in jobs:
        try:
            if jq.release(job_id, token, error=reason, delay_sec=0.0, refund_attempt=True):
                released.append(job_id)
        except Exception:
            log.exception("%s: release at shutdown failed (it will be reclaimed when its lease lapses)", job_id)
            continue
        if on_lost is not None:
            try:
                on_lost(job)
            except Exception:
                log.exception("%s: stopping the released job failed", job_id)
    if released:
        log.warning("released at shutdown: %s", ", ".join(released))
    return released


IMDS = "http://169.254.169.254/latest"
SPOT_CHECK_S = 10.0  # while draining, how often to look for a Spot interruption notice


def spot_interruption_pending(timeout_s: float = 1.0) -> bool:
    """True if EC2 has scheduled a Spot interruption of this instance: IMDS's
    spot/instance-action answers 200 only then (404 otherwise, and off Spot). Off EC2 (a
    laptop, CI) IMDS doesn't answer: False. Pods reach IMDS (hop limit 2)."""
    import urllib.error
    import urllib.request
    try:
        token = urllib.request.urlopen(urllib.request.Request(
            f"{IMDS}/api/token", method="PUT", headers={"X-aws-ec2-metadata-token-ttl-seconds": "60"}),
            timeout=timeout_s).read().decode()
        urllib.request.urlopen(urllib.request.Request(
            f"{IMDS}/meta-data/spot/instance-action", headers={"X-aws-ec2-metadata-token": token}),
            timeout=timeout_s).read()
        return True
    except urllib.error.HTTPError:
        return False  # 404: no interruption scheduled
    except OSError:
        return False  # no IMDS here


class Shutdown:
    """The SIGTERM / SIGINT handler. Stop claiming, and hand the running jobs back now: the
    container is SIGKILLed seconds later, and a job left LEASED would wait out its whole lease.
    With `drain_s` > 0 (SDE_DRAIN_ON_TERM_S: generation, on a deploy) the running jobs may
    finish first, up to that long, then what's left is handed back; a second signal hands
    them back at once. `finished` is set once the claim loops have all returned.

    A Spot interruption is not a deploy: the instance goes ~2 minutes after the notice. When
    `interrupted()` says one is scheduled (spot_interruption_pending: IMDS), the jobs are
    handed back at once, attempt refunded, instead of draining into a dead instance."""

    def __init__(self, stop: threading.Event, drain_s: float = 0.0,
                 interrupted: Callable[[], bool] | None = None):
        self.stop, self.drain_s = stop, drain_s
        self.interrupted = interrupted or spot_interruption_pending
        self.draining, self.finished = threading.Event(), threading.Event()

    def __call__(self, *_: Any) -> None:
        self.stop.set()
        if self.drain_s > 0 and not self.draining.is_set() and self.interrupted():
            log.warning("drain: a Spot interruption is scheduled for this instance; handing back now")
        elif self.drain_s > 0 and not self.draining.is_set():
            self.draining.set()
            with _in_flight_lock:
                running = list(_in_flight)
            log.warning("drain: no new claims; %d running job(s) get up to %.0fs: %s", len(running),
                        self.drain_s, ", ".join(running) or "-")
            threading.Thread(target=self._deadline, name="drain-deadline", daemon=True).start()
            return
        release_in_flight()

    def _deadline(self) -> None:
        # A Spot notice can come while a deploy's drain is under way (no second SIGTERM then):
        # look every SPOT_CHECK_S, and hand back as soon as one is scheduled.
        end = time.monotonic() + self.drain_s
        while not self.finished.wait(min(SPOT_CHECK_S, max(0.0, end - time.monotonic()))):
            if self.interrupted():
                log.warning("drain: a Spot interruption is now scheduled; handing back what still runs")
                release_in_flight("released at a Spot interruption")
                return
            if time.monotonic() >= end:
                log.warning("drain: %.0fs passed; handing back what still runs", self.drain_s)
                release_in_flight("released at the drain deadline")
                return


def run_one(jq: JobQueue, job: dict[str, Any], fn: JobFn, handoff: Handoff | None = None,
            on_start: OnStart | None = None, lease_sec: float = LEASE_TTL_S,
            heartbeat_sec: float = HEARTBEAT_S, on_lost: OnStart | None = None) -> bool:
    """Run one claimed job to a settle (or release). Returns True if it was settled.
    `on_lost(job)` runs when a heartbeat is refused (the task was cancelled, or another
    worker took the job): e.g. stop the job's authoring trial instead of letting it run on."""
    job_id, token = job["id"], job["lease_token"]
    done = threading.Event()
    lost = threading.Event()

    def beat() -> None:
        # A heartbeat that raises (a dropped connection, a failover) is retried, never
        # fatal: a dead heartbeat thread lets the lease lapse under a running job, and the
        # job is then run twice. Only a refused heartbeat (another owner holds the lease)
        # means lost; settle is fenced by the lease token either way.
        wait, failing_since = heartbeat_sec, None
        while not done.wait(wait):
            try:
                held = jq.heartbeat(job_id, token, lease_sec)
            except Exception as e:
                failing_since = failing_since or time.monotonic()
                log.warning("%s: heartbeat failed for %.0fs (lease %.0fs): %s: %s", job_id,
                            time.monotonic() - failing_since, lease_sec, type(e).__name__, str(e)[:200])
                wait = min(heartbeat_sec, HEARTBEAT_RETRY_S)
                continue
            wait, failing_since = heartbeat_sec, None
            if not held:
                lost.set()
                if on_lost is not None:
                    try:
                        on_lost(job)
                    except Exception:
                        log.exception("%s: stopping the lost job failed", job_id)
                return

    threading.Thread(target=beat, name=f"heartbeat-{job_id}", daemon=True).start()
    with _in_flight_lock:
        _in_flight[job_id] = (jq, token, job, on_lost)
    # The heartbeat runs until the settle (or release) is through: a database blip there is
    # retried within the lease (dbretry), and the job stays ours meanwhile.
    try:
        try:
            if on_start is not None:
                on_start(job)
            fields = fn(job)
        except Exception as e:
            log.exception("%s: job function raised", job_id)
            error = f"{type(e).__name__}: {str(e)[:500]}"
            dbretry.retry(lambda: jq.release(job_id, token, error=error, delay_sec=FAILURE_BACKOFF_S),
                          f"{job_id}: release", budget_s=lease_sec)
            return False
        # Settle even when the lease is lost: it is fenced, so it changes nothing then,
        # except that a job closed by a cancel still records what it spent.
        settled = dbretry.retry(lambda: jq.settle(job_id, token, fields, then=handoff),
                                f"{job_id}: settle", budget_s=lease_sec)
    finally:
        done.set()
        with _in_flight_lock:
            _in_flight.pop(job_id, None)
    if not settled or lost.is_set():
        log.warning("%s: lease lost before settling; result dropped", job_id)
        return False
    log.info("%s: %s%s", job_id, fields.get("outcome"),
             f" ({fields['error'][:200]})" if fields.get("error") else "")
    return True


def serve(
    jq: JobQueue,
    job_types: JobType | tuple[JobType, ...] | list[JobType],
    fn: JobFn,
    *,
    handoff: Handoff | None = None,
    on_start: OnStart | None = None,
    concurrency: int = 1,
    docker_slots: int | None = None,
    agent_slots: int | None = None,
    stop: threading.Event | None = None,
    lease_sec: float = LEASE_TTL_S,
    on_lost: OnStart | None = None,
) -> None:
    """Run `concurrency` claim loops until `stop` is set."""
    stop = stop or threading.Event()
    types = [job_types] if isinstance(job_types, str) else list(job_types)
    name = "+".join(map(str, types))
    base = f"{socket.gethostname()}:{os.getpid()}:{name}"

    def loop(owner: str) -> None:
        # Never let an error end this thread: a claim through a database blip is retried, and
        # anything else is logged and the loop goes on (a dead loop is lost capacity, and one
        # that took the process down stranded every running job for its lease).
        while not stop.is_set():
            try:
                job = dbretry.retry(lambda: jq.claim(types, owner, lease_sec, docker_slots, agent_slots),
                                    f"{owner}: claim", stop=stop)
                if job is None:
                    stop.wait(POLL_S)
                    continue
                run_one(jq, job, fn, handoff, on_start, lease_sec=lease_sec, on_lost=on_lost)
            except Exception:
                if stop.is_set():
                    return
                log.exception("%s: claim loop error; going on in %.0fs", owner, LOOP_ERROR_WAIT_S)
                stop.wait(LOOP_ERROR_WAIT_S)

    threads = [threading.Thread(target=loop, args=(f"{base}:{i}",), name=f"{name}-{i}")
               for i in range(concurrency)]
    for t in threads:
        t.start()
    for t in threads:
        while t.is_alive():
            t.join(timeout=0.5)


# --- a service serves several job types -------------------------------------------------
# The job function, the start hook and the hand-off are picked per job by its type.

JobFns = JobFn | dict[str, JobFn]


def dispatch(table: dict[str, Any]) -> Callable[..., Any]:
    """Call the entry for the job's type: f(job) or f(conn, job)."""

    def pick(*args: Any) -> Any:
        job = args[-1]
        return table[job["type"]](*args)

    return pick


def as_job_fn(fn: JobFns) -> JobFn:
    return dispatch(fn) if isinstance(fn, dict) else fn


def settled(out: dict[str, Any]) -> dict[str, Any]:
    """The fields of a job function's result that a settle writes."""
    return {k: out.get(k) for k in ("outcome", "error", "result", "cost_usd")}


def run_service(description: str, roles: list[str],
                start: Callable[[Backend, argparse.Namespace, threading.Event], None]) -> None:
    """A service's command line: `[role] [--concurrency N] [--with-reconciler]`. `roles` are
    the accepted spellings of the service's one role (optional: the module names it).
    `start(backend, args, stop)` runs the service's claim loops until `stop` is set."""
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("role", nargs="?", choices=roles, default=roles[0])
    parser.add_argument("--concurrency", type=int, default=None)
    parser.add_argument("--with-reconciler", action="store_true",
                        help="also run the reconciler in this process (worker-control)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(threadName)s %(levelname)s %(message)s")

    # Starting while the database is away waits for it (nothing is running yet).
    backend = dbretry.retry(from_env, "connecting to the backends", budget_s=STARTUP_WAIT_S)
    log.info("backends: %s", describe(backend))
    stop = threading.Event()
    shutdown = Shutdown(stop, DRAIN_ON_TERM_S)
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, shutdown)
    if args.with_reconciler:
        reconciler.start_thread(backend)

    try:
        start(backend, args, stop)
    finally:
        # However this process ends, hand back what it still runs (a no-op after a shutdown).
        stop.set()
        shutdown.finished.set()
        release_in_flight("released at worker exit")
