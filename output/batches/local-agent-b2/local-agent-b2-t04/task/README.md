# Retry cache commit order workflow

A Python order-fulfillment worker caches reservation results before committing order status. After a worker is killed and restarted, retries can leave orders accepted with no stored result or reserve duplicate packages. The task is to fix the retry/cache/commit interaction so all order invariants hold.

## Scenario

The service has an order store, a non-idempotent fulfillment provider, and a response cache. A worker processes queued orders, reserves packages, caches results, and then commits fulfillment. Users see accepted orders stuck after the worker has already reserved a package and announced a result.

## Root cause (hidden from the agent)

service.py calls cache.put before store.mark_fulfilled. On retry, cache.get causes store.finish_cached to delete the queue entry without settling the order. The reference fix settles the cached result into the order store atomically on retry. The plausible wrong fix disables the cache, causing provider.reserve to create duplicate packages after a crash.

## Correctness constraints

- **C1**: Each order reserves at most one fulfillment package.
- **C2**: An order with no queued work is fulfilled.
- **C3**: A fulfilled order records its reservation result.
- **C4**: The order result identifies the reservation package.
- **C5**: Cached and stored results agree.

## Subgoals

1. Ensure cached fulfillment results are committed to the order store on retry.
2. Preserve at-most-one reservation across worker kills and restarts.
3. Keep submit, worker, status, and report behavior working.

## Failure scenario

Tests hold an exclusive order-database lock, start a worker, kill it after it emits result_ready, release the lock, restart the worker, and report the order. The original code leaves the order accepted with no stored result; the wrong fix reserves a second package.

## Expected agent pitfalls

- Disable the retry cache to force re-reservation, breaking at-most-one package.
- Only delete the queue entry on cache hit, leaving the order accepted.
- Assume the fulfillment provider is idempotent.
- Add process-local state that does not survive restarts.

## Anti-shortcut measures

- Tests kill a worker after result_ready and restart it over 10 fixed seeds.
- Tests inspect package_count, order result, queue state, and cache/store agreement.
- The wrong fix is rejected by the at-most-one-package invariant.

## Requirement-to-test coverage

| Requirement | Tests |
|---|---|
| C1 | `test_invariant_1_each_order_reserves_at_most_one_fulfillment_package`, `test_exact_counterexample_replay` |
| C2 | `test_invariant_2_order_with_no_queued_work_is_fulfilled`, `test_exact_counterexample_replay` |
| C3 | `test_invariant_3_fulfilled_order_records_its_reservation_result`, `test_exact_counterexample_replay` |
| C4 | `test_invariant_4_the_order_result_identifies_the_reservation_package`, `test_exact_counterexample_replay` |
| C5 | `test_invariant_5_cached_and_stored_results_agree`, `test_exact_counterexample_replay` |
