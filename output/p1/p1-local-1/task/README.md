# Durable reservation retry

A Python reservation queue stores orders and inventory in separate SQLite databases. Delivery retries after interrupted workers can apply an inventory effect more than once. The task is to make request-ID effects durable and idempotent while preserving queue recovery and CLI behavior.

## Scenario

The agent receives a realistic CLI service with durable orders, expiring worker leases, and a separate inventory ledger. The visible symptom is over-debiting and false stock exhaustion after retries.

## Root cause (hidden from the agent)

Inventory reserve is atomic only within the inventory database and has no durable request-ID result reuse. A worker can commit an inventory debit and exit before the separate order-completion commit; lease recovery calls reserve again and inserts another ledger movement.

## Correctness constraints

- **C1**: A request ID produces at most one accepted inventory movement, with quantity no greater than its submitted quantity, and every accepted movement fits durable stock at that moment; stock never becomes negative.
- **C2**: Once a request ID has produced a movement, later deliveries reuse that exact durable inventory result and create no additional effect.
- **C3**: An expired lease is retried without losing the order, preserving availability and any prior durable inventory effect.
- **C4**: Normal CLI behavior remains intact: valid requests are queued and fulfilled exactly once when stock fits; insufficient stock is rejected without an inventory effect; repeated identical submissions return the existing order; conflicts and invalid inputs fail; idle workers, logical time, and inspect formats work as documented.

## Subgoals

1. Understand the durable boundary between inventory effects and order completion.
2. Make the externally supplied request ID the durable inventory idempotency boundary.
3. Ensure atomic stock decisions under concurrent or retrying workers.
4. Preserve normal queue, rejection, and CLI behavior.

## Failure scenario

Initialize 14 units, submit order-1 for 7, run a worker with STOP_AFTER=inventory, advance logical time by 30001 milliseconds, run a replacement worker, then submit and process order-2 for 7. The broken service creates two movements for order-1 and rejects order-2; a correct service has one movement for each and zero stock.

## Expected agent pitfalls

- A worker-local deduplication set does not survive the process restart that performs redelivery.
- Only marking the order complete ignores inventory already committed before a crash.
- A unique key without reusing the prior durable result can make recovery fail even though the effect exists.
- Blocking retries avoids duplicate effects but violates availability and recovery guarantees.

## Anti-shortcut measures

- The primary check recreates a deterministic interrupted-worker and expired-lease schedule and verifies both ledger effects and final availability.
- A fixed-seed check explores additional submissions, workers, crashes, lease expirations, and retries and verifies exact global stock accounting.
- Regression checks reject changes that break submission idempotency, normal fulfillment, rejection, idle workers, clock behavior, or CLI output.
- The process-local wrong fix is exercised across new worker processes and cannot satisfy the restart-sensitive guarantee.

## Requirement-to-test coverage

| Requirement | Tests |
|---|---|
| C1 | `test_expired_lease_recovery_preserves_effect_and_availability`, `test_repeated_crash_redelivery_reuses_durable_result`, `test_fixed_seed_failure_schedules_preserve_accounting`, `test_insufficient_stock_is_rejected_without_effect`, `test_multiple_skus_are_independent` |
| C2 | `test_expired_lease_recovery_preserves_effect_and_availability`, `test_repeated_crash_redelivery_reuses_durable_result`, `test_fixed_seed_failure_schedules_preserve_accounting` |
| C3 | `test_expired_lease_recovery_preserves_effect_and_availability`, `test_repeated_crash_redelivery_reuses_durable_result`, `test_fixed_seed_failure_schedules_preserve_accounting` |
| C4 | `test_happy_path_clock_and_idle_worker_regression`, `test_submission_idempotency_and_conflict_semantics`, `test_insufficient_stock_is_rejected_without_effect`, `test_invalid_inputs_do_not_change_durable_state`, `test_multiple_skus_are_independent` |
