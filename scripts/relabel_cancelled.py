"""
Relabel tasks an operator stopped by rejecting them (before POST …/cancel existed) as
what they are: CANCELLED, so reports stop counting them as infra failures.

    python scripts/relabel_cancelled.py --task-ids aws-b1-t01 aws-b1-t02 ...            # dry run
    python scripts/relabel_cancelled.py --task-ids ... --execute [--note "stopped aws-b1"]

Only a task that is REJECTED with reason infra_unresolved changes (shared/jobs/tasks.py
relabel_as_cancelled); anything else is listed and left alone. Its jobs were already
closed by the reject. Uses DATABASE_URL like every service. One transaction.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shared.jobs import tasks  # noqa: E402
from shared.storage import from_env  # noqa: E402
from shared.contracts.models import RejectReason, TaskStatus  # noqa: E402


def main(argv: list[str] | None = None, backend=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.strip().split("\n\n")[0])
    ap.add_argument("--task-ids", nargs="+", required=True)
    ap.add_argument("--execute", action="store_true", help="apply (default: dry run)")
    ap.add_argument("--by", default="operator")
    ap.add_argument("--note", default="")
    args = ap.parse_args(argv)
    backend = backend or from_env()

    changed = 0
    with backend.store.tx() as conn:
        for tid in args.task_ids:
            row = tasks.task(conn, tid)
            if row is None:
                print(f"{tid}: not found, skipped")
                continue
            eligible = row["status"] == TaskStatus.REJECTED and row["reason"] == RejectReason.INFRA_UNRESOLVED
            if not eligible:
                print(f"{tid}: {row['status']} ({row['reason']}), not an infra_unresolved reject, skipped")
                continue
            if not args.execute:
                print(f"{tid}: REJECTED (infra_unresolved) at {row['updated_at']} -> would become CANCELLED")
                continue
            if tasks.relabel_as_cancelled(conn, tid, args.by, args.note):
                changed += 1
                print(f"{tid}: REJECTED (infra_unresolved) -> CANCELLED")
    print(f"{changed} relabelled" if args.execute else "dry run: nothing changed (pass --execute to apply)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
