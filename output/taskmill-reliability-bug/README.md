# Fix Taskmill Worker Reliability Bug

Fix a reliability bug in the taskmill background job processor where worker crash between acknowledgment and result persistence causes silent job loss, and blind recovery causes duplicate effects

## Scenario

Worker crash between job acknowledgment and result persistence causes permanent silent data loss; recovery of jobs that already have results causes duplicate-write integrity errors

## Root cause (hidden from the agent)

In worker.py process_job(), queue.complete_job() is called before store.write_result(). In recovery.py recover(), all processing jobs are reset to pending without checking store.result_exists().

## Correctness constraints

- **no_silent_loss**: Every enqueued job must eventually have exactly one result in the output store. A job marked as completed must always have a corresponding result row.
- **no_duplicate_effects**: Each job must produce exactly one result entry. Recovery of interrupted jobs must not cause duplicate writes or integrity errors.
- **crash_recovery**: If a worker crashes at any point during job processing, all affected jobs must eventually be completed with correct results after recovery is run and the worker is restarted.

## Subgoals

1. Fix the order of operations in worker.py so that results are persisted before jobs are acknowledged, with idempotency checking to handle re-processing
2. Fix recovery.py so that processing jobs with existing results are completed directly rather than reset for re-processing

## Failure scenario

A worker processes a job by marking it completed in the queue database before writing the computed result. If the worker crashes between these two operations, the job appears completed but has no result. The recovery mechanism only examines jobs in 'processing' state, so it never detects or repairs the inconsistency. Separately, recovery blindly resets all processing jobs to pending without checking whether a result already exists, causing duplicate-write errors on retry.

## Expected agent pitfalls

- Only fixing the ordering in worker.py without fixing recovery.py will still fail test_no_duplicate_effects
- Wrapping complete+write in a single transaction without reordering does not prevent silent loss
- Deleting and re-inserting results bypasses UNIQUE but violates the no-duplicate-effects guarantee

## Anti-shortcut measures

- Tests check actual database state after crash+recovery, not just process exit codes
- UNIQUE constraint on results.job_id prevents silently inserting duplicate rows
- Direct database manipulation in tests sets up crash-equivalent states that bypass any single-point fix
- fail_job overwrites completed status, so simply suppressing exceptions without fixing ordering causes jobs to appear failed

## Requirement-to-test coverage

| Requirement | Tests |
|---|---|
| no_silent_loss | `test_no_silent_loss`, `test_crash_recovery` |
| no_duplicate_effects | `test_no_duplicate_effects`, `test_recovery_handles_mixed_states` |
| crash_recovery | `test_crash_recovery`, `test_recovery_handles_mixed_states` |
