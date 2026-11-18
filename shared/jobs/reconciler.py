"""The reconciler: periodic repair of job state (PRD §5.1). Runs on worker-control only.

Each pass:

1. Dead-letters jobs that were claimed MAX_JOB_ATTEMPTS times without settling
   (each lease expired: the job keeps killing its worker). They become DEAD with
   `handled_at` NULL. A dead rollout also gets `<id>-analyze` in the same
   transaction, the follow-up its worker never settled, so the Trace Parser
   retries the slot or resolves the version instead of waiting forever. A dead
   create / revise / validate job rejects its task (`infra_unresolved`): no
   service would ever hand that version on.
2. Re-adds every claimable job to the queue backend (Redis mode), which covers a
   crash between commit and publish, a dead worker's popped job, and Redis
   losing its data. A no-op for the database queue.
3. Deletes artifact staging entries (`.staging/`) older than the lease TTL: the
   half-written copies of a process killed mid-write.

Expired leases need no action here: the claim query takes them over directly.
Every step is a compare-and-set, so an accidental second reconciler is harmless.

Run standalone with `python -m shared.jobs.reconciler`, or inside a worker process
with `start_thread(backend)`.
"""

import argparse
import logging
import os
import threading
import time
from pathlib import Path

from shared.jobs import tasks
from shared.storage import REPO_ROOT, Backend, describe, from_env
from shared.storage.artifacts import sweep_staging
from shared.storage.sql import insert_job, utcnow
from shared.jobs.jobqueue import JobQueue
from shared.contracts.models import (
    GENERATION_TYPES,
    LEASE_TTL_S,
    MAX_JOB_ATTEMPTS,
    RECONCILE_S,
    JobType,
    RejectReason,
    Stage,
)

# Where accepted bundles are published; the Trace Parser reads the same variable.
ACCEPTED_DIR = Path(os.environ.get("SDE_ACCEPTED_DIR") or REPO_ROOT / "output" / "accepted")

log = logging.getLogger("reconciler")


def reconcile(backend: Backend, accepted_dir: Path = ACCEPTED_DIR) -> dict[str, int]:
    analyze: list[str] = []
    with backend.store.tx() as c:
        dead = c.execute(
            """UPDATE jobs SET status = 'DEAD', finished_at = ?, lease_until = NULL,
                   error = 'dead-lettered: claimed ' || attempts || ' times without settling'
                WHERE attempts >= ?
                  AND (status = 'READY' OR (status = 'LEASED' AND lease_until < ?))
            RETURNING id, type, task_id, version""",
            (utcnow(), MAX_JOB_ATTEMPTS, time.time()),
        ).fetchall()
        for row in dead:
            if row["type"] == JobType.ROLLOUT:
                aid = f"{row['id']}-analyze"
                if insert_job(c, id=aid, task_id=row["task_id"], version=row["version"],
                              type=JobType.ANALYZE, input={"trigger": row["id"]}):
                    analyze.append(aid)
            elif row["type"] in (*GENERATION_TYPES, JobType.VALIDATE):
                # No service will ever hand this version on: the task can't finish.
                tasks.reject(c, row["task_id"], RejectReason.INFRA_UNRESOLVED, Stage.UNRESOLVED,
                             version=row["version"])
    for row in dead:
        log.warning("%s: dead-lettered after %d claims", row["id"], MAX_JOB_ATTEMPTS)
    JobQueue(backend).publish(analyze)
    republished = backend.queue.reconcile()
    swept = sweep_staging([*backend.artifacts.staging_dirs(), accepted_dir], LEASE_TTL_S)
    return {"dead_lettered": len(dead), "republished": republished, "staging_swept": swept}


def run(backend: Backend, stop: threading.Event, interval_s: float = RECONCILE_S) -> None:
    while not stop.is_set():
        try:
            counts = reconcile(backend)
            if counts["dead_lettered"]:
                log.info("reconcile: %s", counts)
        except Exception:  # a database or Redis blip: try again next pass
            log.exception("reconcile pass failed")
        stop.wait(interval_s)


def start_thread(backend: Backend, interval_s: float = RECONCILE_S) -> threading.Event:
    """Run the reconciler in a daemon thread. Set the returned event to stop it."""
    stop = threading.Event()
    threading.Thread(target=run, args=(backend, stop, interval_s), name="reconciler",
                     daemon=True).start()
    return stop


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the job reconciler.")
    parser.add_argument("--once", action="store_true", help="one pass, print counts, exit")
    parser.add_argument("--interval", type=float, default=RECONCILE_S)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    backend = from_env()
    log.info("backends: %s", describe(backend))
    if args.once:
        print(reconcile(backend))
        return
    run(backend, threading.Event(), args.interval)


if __name__ == "__main__":
    main()
