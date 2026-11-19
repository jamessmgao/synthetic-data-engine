# Fix Reliability Bugs in Taskmill Job Processor

## Background

Taskmill is a lightweight background job processing service. It uses a SQLite database to manage a job queue and store job results. Workers pick up pending jobs, execute handlers to compute results, and persist those results to the database.

Some job types produce side effects in addition to returning a result. For example, the **notify** handler records a notification row in the database when it runs. Other handlers (echo, compute, transform) are pure computations with no side effects.

## Problem

The system has reliability bugs. When a worker process crashes during job processing, jobs can end up in an inconsistent state:

1. **Silent data loss**: Some completed jobs have no corresponding result in the output store. These jobs are permanently lost — the recovery mechanism does not detect or repair them.
2. **Duplicate side effects on recovery**: When the recovery mechanism resets interrupted jobs for re-processing, handlers that produce side effects can be invoked again, causing duplicate side-effect records (e.g., duplicate notification rows for the same job).
3. **Incomplete recovery**: Jobs that were mid-processing when a crash occurred may not be properly recovered and completed.

## Required Guarantees

After your fix, the system must satisfy these correctness properties:

1. **No silent loss**: Every job that is enqueued must eventually have exactly one result in the output store. A job marked as "completed" must always have a corresponding result.
2. **No duplicate effects**: Each job must produce exactly one result entry and, for job types with side effects (such as "notify"), exactly one side-effect record per job. Recovery of interrupted jobs must not cause duplicate writes of any kind.
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
  - `notifications [--job-id <id>]` — List notifications

## Diagnostic Crash Injection

The system includes crash-injection environment variables for validating reliability behavior. These must be preserved in your fix:

- `TASKMILL_CRASH_AFTER_HANDLER=<job_id>` — Worker calls `os._exit(1)` immediately after the handler returns for the specified job (before any status or result changes by the worker)
- `TASKMILL_CRASH_AFTER_COMPLETE=<job_id>` — Worker calls `os._exit(1)` after marking the specified job as completed
- `TASKMILL_CRASH_AFTER_RESULT=<job_id>` — Worker calls `os._exit(1)` after writing the result for the specified job

Example usage:

```
TASKMILL_CRASH_AFTER_HANDLER=1 python3 -m taskmill --db /var/lib/taskmill/taskmill.db worker
```
