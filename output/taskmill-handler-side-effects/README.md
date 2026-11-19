# Fix Taskmill Worker Reliability and Handler Side-Effect Bugs

Fix reliability bugs in the taskmill background job processor where worker crash between acknowledgment and result persistence causes silent job loss, the notify handler writes side effects through a separate database connection making them non-atomic with result writes, and recovery blindly resets all processing jobs without checking for existing results

## Scenario

Worker crash between job acknowledgment and result persistence causes permanent silent data loss; notify handler commits side effects in a separate transaction so recovery of interrupted jobs causes duplicate notification rows; recovery mechanism does not check for already-written results before resetting jobs

## Root cause (hidden from the agent)

In worker.py process_job(), queue.complete_job() is called before store.write_result(). In handlers.py _handle_notify(), the handler opens its own database connection and commits side effects independently of the worker's transaction, with no idempotency guard. In recovery.py recover(), all processing jobs are blindly reset to pending without checking store.result_exists().

## Correctness constraints

- **no_silent_loss**: Every enqueued job must eventually have exactly one result in the output store. A job marked as completed must always have a corresponding result row.
- **no_duplicate_effects**: Each job must produce exactly one result entry and, for job types with side effects (notify), exactly one side-effect record per job. Recovery of interrupted jobs must not cause duplicate writes of any kind.
- **crash_recovery**: If a worker crashes at any point during job processing, all affected jobs must eventually be completed with correct results after recovery is run and the worker is restarted.

## Subgoals

1. Fix the order of operations in worker.py so that results are persisted before jobs are acknowledged, with an idempotency check to handle re-processing of jobs that already have results
2. Fix the notify handler in handlers.py to be idempotent so that re-invocation after recovery does not produce duplicate side-effect records
3. Fix recovery.py so that processing jobs with existing results are completed directly rather than reset for re-processing

## Failure scenario

A worker processes a job by marking it completed in the queue database before writing the computed result. If the worker crashes between these two operations, the job appears completed but has no result. The recovery mechanism only resets processing jobs to pending, so it never detects completed-without-result jobs. Separately, the notify handler opens its own database connection and writes notification side effects in a separate transaction from the worker's result write and job completion. If the worker crashes after the handler runs but before the result is written, the notification is committed but the result is not. Recovery resets the job to pending, the worker re-processes it, and the handler writes a duplicate notification. Because the notifications table lacks a UNIQUE constraint on job_id, there is no database-level guard against this duplication.

## Expected agent pitfalls

- Only fixing the write ordering in worker.py without making the notify handler idempotent will still fail test_no_duplicate_notifications
- Only adding an idempotency check on results without fixing the handler will fail because re-invoked handlers produce duplicate notification rows
- Using INSERT OR IGNORE in the handler without a UNIQUE constraint on notifications.job_id does not prevent duplicates
- Only fixing recovery to check result_exists without fixing the handler will still cause duplicate notifications on re-processing
- Wrapping complete+write in a single transaction without reordering does not prevent silent loss

## Anti-shortcut measures

- Tests check actual database state after crash+recovery, not just process exit codes
- UNIQUE constraint on results.job_id prevents silently inserting duplicate result rows
- No UNIQUE constraint on notifications.job_id means INSERT OR IGNORE alone does not prevent duplicate notifications
- Direct database manipulation in tests sets up crash-equivalent states that bypass any single-point fix
- fail_job overwrites completed status, so suppressing exceptions without fixing ordering causes jobs to appear failed

## Requirement-to-test coverage

| Requirement | Tests |
|---|---|
| no_silent_loss | `test_no_silent_loss`, `test_crash_recovery` |
| no_duplicate_effects | `test_no_duplicate_effects`, `test_no_duplicate_notifications`, `test_recovery_with_existing_side_effects` |
| crash_recovery | `test_crash_recovery`, `test_recovery_with_existing_side_effects` |
