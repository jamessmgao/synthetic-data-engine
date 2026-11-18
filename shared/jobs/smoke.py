"""End-to-end check of the configured backends, safe to run against production.

Creates a throwaway task `smoke-<time>` and its job, claims it through the
queue (Redis if configured), settles it, round-trips a bundle and a trace through
the artifact store, runs one reconciler pass, then deletes its own rows. It prints
one JSON line and exits non-zero on any failure.

    python -m shared.jobs.smoke
"""

import json
import os
import socket
import sys
import tempfile
import time
from pathlib import Path

from shared.storage import describe, from_env
from shared.storage.sql import insert_job, utcnow
from shared.jobs.jobqueue import JobQueue
from shared.contracts.models import JobType
from shared.jobs.reconciler import reconcile


def main() -> int:
    t0 = time.time()
    backend = from_env()
    report = {"backends": describe(backend), "release": os.environ.get("SDE_RELEASE")}
    jq = JobQueue(backend)
    task = f"smoke-{int(t0)}"
    jid = f"{task}-v1-smoke"
    try:
        with backend.store.tx() as c:
            now = utcnow()
            c.execute("""INSERT INTO tasks (id, status, stage, created_at, updated_at)
                         VALUES (?, 'CALIBRATING', 'QUEUED', ?, ?)""", (task, now, now))
            insert_job(c, id=jid, task_id=task, version=1, type=JobType.SMOKE)
        jq.publish([jid])

        owner = f"{socket.gethostname()}:{os.getpid()}:smoke"
        job = jq.claim("smoke", owner)  # only smoke jobs are of this type
        assert job and job["id"] == jid, "claim through the queue failed"
        assert jq.heartbeat(jid, job["lease_token"]), "heartbeat failed"
        assert jq.settle(jid, job["lease_token"], {"outcome": "OK", "result": {"smoke": True}})
        assert backend.store.job(jid)["status"] == "DONE"
        report["queue"] = "ok"

        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "task"
            src.mkdir()
            (src / "instruction.md").write_text(f"smoke {task}\n")
            sha = backend.artifacts.put_bundle(src)
        assert (backend.artifacts.bundle_path(sha) / "instruction.md").read_text() == f"smoke {task}\n"
        trace = backend.artifacts.trace_dir(jid)
        trace.mkdir(parents=True, exist_ok=True)
        (trace / "result.json").write_text("{}")
        report["trace_uri"] = backend.artifacts.save_trace(jid)
        assert backend.artifacts.fetch_trace(jid)
        report["artifacts"] = "ok"

        report["reconcile"] = reconcile(backend)
    except Exception as e:
        report["error"] = f"{type(e).__name__}: {e}"
    finally:
        with backend.store.tx() as c:
            c.execute("DELETE FROM jobs WHERE task_id = ?", (task,))
            c.execute("DELETE FROM tasks WHERE id = ?", (task,))
    report["seconds"] = round(time.time() - t0, 2)
    report["ok"] = "error" not in report
    print(json.dumps(report))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
