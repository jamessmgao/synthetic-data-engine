# Ledger dedup cache loss on restart

A Go ledger service uses an in-memory dedup cache that is lost on restart, allowing retried transactions to debit accounts twice. Startup recovery of persisted pending transactions also bypasses the cache.

## Scenario

The agent is given a Go ledger service with an in-memory dedup cache. The service processes debit transactions against named accounts, persisting state in SQLite. On restart, the cache is empty, so retried transaction keys are not recognized as already processed.

## Root cause (hidden from the agent)

The DedupCache in handler.go is process-local (in-memory only). When the service restarts, the cache is empty. Additionally, the Recover() function in ledger.go processes pending transactions without populating the cache, so recovered keys are never added to it.

## Correctness constraints

- **C1**: Each transaction key changes its account balance at most once, including across service restarts and startup recovery.

## Subgoals

1. Identify that the in-memory dedup cache is lost on restart
2. Identify that startup recovery bypasses the dedup cache
3. Add a persistent check (e.g., database lookup) before creating a new pending transaction

## Failure scenario

Repro: submit a transaction, restart the server, retry the same key. The balance is debited twice. Also: insert a pending record, restart (recovery debits once), retry the key; debited again.

## Expected agent pitfalls

- Persisting the cache to disk handles the simple restart case but fails for recovered transactions whose keys are never added to the cache
- Adding keys to the cache only during HTTP handling misses the recovery path

## Anti-shortcut measures

- Tests kill and restart the server, insert pending records, and retry keys to verify the invariant holds across all paths
- The wrong fix (persistent cache without recovery population) is explicitly tested and must fail

## Requirement-to-test coverage

| Requirement | Tests |
|---|---|
| C1 | `test_invariant_1_at_most_one_debit_per_key`, `test_counterexample_exact`, `test_regression_happy_path` |
