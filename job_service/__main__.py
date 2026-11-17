"""Run the Job Service.

    python -m job_service submit --count 10 [--batch ID]
    python -m job_service run [--batch ID] [--once | --until-done]

Backends come from the environment (DATABASE_URL, REDIS_URL, S3_*, SDE_WORK_DIR);
see shared/storage/__init__.py. Any number of copies can run at once.
"""

import argparse
import json
import signal
import sys
import threading

from shared.storage import from_env

from job_service.service import BatchConflict, JobService


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m job_service")
    sub = parser.add_subparsers(dest="command", required=True)
    submit = sub.add_parser("submit", help="create a batch of candidate tasks")
    submit.add_argument("--count", type=int, default=10)
    submit.add_argument("--batch")
    run = sub.add_parser("run", help="advance tasks as workers settle their jobs")
    run.add_argument("--batch")
    mode = run.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="one pass, then exit")
    mode.add_argument("--until-done", action="store_true", help="exit when every task is final")
    args = parser.parse_args()

    svc = JobService(from_env())
    if args.command == "submit":
        try:
            batch_id, task_ids, created = svc.submit_batch(args.count, args.batch)
        except (BatchConflict, ValueError) as e:
            sys.exit(f"error: {e}")
        print(json.dumps({"batch_id": batch_id, "task_ids": task_ids, "created": created}))
    elif args.once:
        print(f"{svc.reconcile(args.batch)} task(s) advanced")
    else:
        stop = threading.Event()
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
        try:
            svc.run(args.batch, until_done=args.until_done, stop=stop)
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
