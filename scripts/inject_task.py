"""
Test the pipeline past generation: hand an existing task directory to Build + Validate.

Generation quality shouldn't block testing the rest of the system. This puts a
task directory (a bundle another run generated, or any Harbor task) into the
artifact store and creates a task row with exactly the `validate` job the
Generation Service would have queued for it. From there everything is real:
the gates, five rollouts, the Trace Parser's review and its decision.

    python scripts/inject_task.py <task_dir> <task_id> [--draft <draft record .json>]

Pass the generator's draft record (work/drafts/<sha>.json from the run that made
the bundle) to let repairs and revisions work: they continue GLM-5.1's own draft,
and the Trace Parser reads the hidden spec from it. Without one, the task can
still be validated and evaluated, but a failed gate or an out-of-band verdict
ends it, because there is no draft to change.

Backends come from the environment (DATABASE_URL, REDIS_URL, S3_*, SDE_WORK_DIR),
the same variables the services use; SDE_WORK_DIR must be the services' work dir.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shared.storage import from_env  # noqa: E402
from shared.storage.sql import insert_job, job_id, utcnow  # noqa: E402
from shared.jobs.jobqueue import JobQueue  # noqa: E402
from shared.contracts.models import JobType, Stage, TaskStatus  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[1].strip())
    parser.add_argument("task_dir", type=Path)
    parser.add_argument("task_id")
    parser.add_argument("--draft", type=Path, help="the generator's draft record for this bundle")
    args = parser.parse_args()

    backend = from_env()
    task_dir = args.task_dir.resolve()
    sha = backend.artifacts.put_bundle(task_dir)
    if args.draft:
        drafts = backend.work_dir / "drafts"
        drafts.mkdir(parents=True, exist_ok=True)
        (drafts / f"{sha}.json").write_text(args.draft.read_text())

    vid = job_id(args.task_id, 1, "validate", 0)
    with backend.store.tx() as conn:
        if conn.execute("SELECT 1 FROM tasks WHERE id = ?", (args.task_id,)).fetchone():
            sys.exit(f"task {args.task_id!r} already exists")
        now = utcnow()
        conn.execute(
            """INSERT INTO tasks (id, slot, status, current_version, stage, bundle_sha256, name,
                                  created_at, updated_at)
               VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?)""",
            (args.task_id, json.dumps({"injected_from": str(task_dir)}), str(TaskStatus.CALIBRATING),
             str(Stage.BUILDING), sha, task_dir.name, now, now),
        )
        insert_job(conn, id=vid, task_id=args.task_id, version=1, type=JobType.VALIDATE, bundle_sha256=sha,
                   input={"bundle_sha256": sha, "bundle_dir": str(backend.artifacts.bundle_path(sha)), "round": 0})
    JobQueue(backend).publish([vid])
    print(json.dumps({"task_id": args.task_id, "bundle_sha256": sha, "validate_job": vid}))


if __name__ == "__main__":
    main()
