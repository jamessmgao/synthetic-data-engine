# Fix Reliability Bug in Taskmill Job Processor

## Background

Taskmill is a lightweight background job processing service. It uses a SQLite database to manage a job queue and store job results. Workers pick up pending jobs, execute handlers to compute results, and persist those results to the database.

## Problem

The system has a reliability bug. When a worker process crashes during job processing, some jobs end up in an inconsistent state:

1. **Silent data loss**: Some completed jobs have no corresponding result in the output store. These jobs are permanently lost — the recovery mechanism does not detect or repair them.
2. **Duplicate effects on recovery**: When the recovery mechanism resets interrupted jobs for re-processing, it can cause results to be written more than once, leading to integrity errors or incorrect output counts.
3. **Incomplete recovery**: Jobs that were mid-processing when a crash occurred may not be properly recovered and completed.

## Required Guarantees

After your fix, the system must satisfy these correctness properties:

1. **No silent loss**: Every job that is enqueued must eventually have exactly one result in the output store. A job marked as "completed" must always have a corresponding result.
2. **No duplicate effects**: Each job must produce exactly one result entry. Recovery of interrupted jobs must not cause duplicate writes.
3. **Crash recovery**: If a worker crashes at any point during job processing, all affected jobs must eventually be completed with correct results after recovery is run and the worker is restarted.

## System Layout

- **Source code**: `/app/taskmill/`
- **Database**: Configured via `TASKMILL_DB` environment variable (default: `/var/lib/taskmill/taskmill.db`)
- **CLI**: `python3 -m taskmill --db <path> <command>`
  - `enqueue <type> <payload>` — Add a job to the queue
  - `worker` — Process pending jobs
  - `recover` — Recover interrupted jobs
  - `status` — Show queue status
  - `result <job_id>` — Show a job's result

## Key Files to Investigate

- `taskmill/worker.py` — Job processing logic
- `taskmill/recovery.py` — Crash recovery logic
- `taskmill/queue.py` — Job queue operations
- `taskmill/store.py` — Result storage operations (includes `result_exists` helper)

## Diagnostic Crash Injection

The system includes crash-injection environment variables for validating reliability behavior. These must be preserved in your fix:

- `TASKMILL_CRASH_AFTER_COMPLETE=<job_id>` — Worker calls `os._exit(1)` after marking the specified job as completed
- `TASKMILL_CRASH_AFTER_RESULT=<job_id>` — Worker calls `os._exit(1)` after writing the result for the specified job

Example usage:

```
TASKMILL_CRASH_AFTER_COMPLETE=1 python3 -m taskmill --db /var/lib/taskmill/taskmill.db worker
```

## Approach

Investigate the order of operations in the worker's `process_job` method and the recovery logic in `recovery.py`. Consider what happens if the worker crashes at each point between operations, and whether the current code ensures the stated guarantees in every crash scenario. The `result_exists` method in `store.py` is available as an idempotency mechanism.
