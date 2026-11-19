"""Command-line interface for taskmill."""

import argparse
import json
import logging
import sys

from .config import Config
from .db import Database
from .logging import configure_logging
from .notifications import NotificationStore
from .queue import JobQueue
from .recovery import RecoveryManager
from .store import ResultStore
from .worker import Worker


def cmd_enqueue(args):
    config = Config(db_path=args.db)
    db = Database(config)
    queue = JobQueue(db)
    job_id = queue.enqueue(args.type, args.payload)
    print(f"Enqueued job {job_id}")
    db.close()


def cmd_worker(args):
    config = Config(db_path=args.db)
    configure_logging(args.verbose)
    worker = Worker(config)
    processed = worker.run(max_jobs=args.max_jobs)
    print(f"Processed {processed} jobs")
    worker.close()


def cmd_recover(args):
    config = Config(db_path=args.db)
    configure_logging(args.verbose)
    recovery = RecoveryManager(config)
    count = recovery.recover()
    print(f"Recovered {count} jobs")
    recovery.close()


def cmd_status(args):
    config = Config(db_path=args.db)
    db = Database(config)
    queue = JobQueue(db)
    for status in ["pending", "processing", "completed", "failed"]:
        jobs = queue.find_by_status(status)
        print(f"{status}: {len(jobs)}")
    db.close()


def cmd_result(args):
    config = Config(db_path=args.db)
    db = Database(config)
    store = ResultStore(db)
    result = store.get_result(args.job_id)
    if result:
        print(result["output"])
    else:
        print("No result found", file=sys.stderr)
        sys.exit(1)
    db.close()


def cmd_notifications(args):
    config = Config(db_path=args.db)
    db = Database(config)
    store = NotificationStore(db)
    notifications = store.list_notifications(job_id=args.job_id)
    if not notifications:
        print("No notifications found")
    else:
        for n in notifications:
            print(f"Job {n['job_id']}: {n['message']}")
    db.close()


def main():
    parser = argparse.ArgumentParser(prog="taskmill", description="Background job processor")
    parser.add_argument("--db", default=None, help="Database path")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose output")

    subparsers = parser.add_subparsers(dest="command")

    p_enqueue = subparsers.add_parser("enqueue", help="Enqueue a new job")
    p_enqueue.add_argument("type", help="Job type")
    p_enqueue.add_argument("payload", help="Job payload")

    p_worker = subparsers.add_parser("worker", help="Run the worker")
    p_worker.add_argument("--max-jobs", type=int, default=None, help="Max jobs to process")

    p_recover = subparsers.add_parser("recover", help="Recover interrupted jobs")

    p_status = subparsers.add_parser("status", help="Show queue status")

    p_result = subparsers.add_parser("result", help="Show job result")
    p_result.add_argument("job_id", type=int, help="Job ID")

    p_notif = subparsers.add_parser("notifications", help="List notifications")
    p_notif.add_argument("--job-id", type=int, default=None, help="Filter by job ID")

    args = parser.parse_args()

    if args.command == "enqueue":
        cmd_enqueue(args)
    elif args.command == "worker":
        cmd_worker(args)
    elif args.command == "recover":
        cmd_recover(args)
    elif args.command == "status":
        cmd_status(args)
    elif args.command == "result":
        cmd_result(args)
    elif args.command == "notifications":
        cmd_notifications(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
