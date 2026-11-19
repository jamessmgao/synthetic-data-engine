# Ledger Reconciliation Memory Optimization

A Java ledger reconciler runs out of memory when processing a 6M-event ledger under a 128 MiB heap. The agent must optimize memory usage while preserving byte-identical report output including timestamp-ordered checksums.

## Scenario

A financial ledger reconciliation tool that processes event logs and produces account summaries. The tool fails with OutOfMemoryError on large ledgers when heap is constrained to 128 MiB.

## Root cause (hidden from the agent)

Reconciler.reconcile uses EventLog.snapshot which materializes all events into a LinkedList, causing OOM on large ledgers. Additionally, it computes account checksums in ledger (sequence) order rather than the required timestamp-then-sequence order.

## Correctness constraints

- **C1**: A valid 6000000-event ledger reconciliation completes within 75 seconds while the Java process is limited to a 128 MiB heap.
- **C2**: For the deterministic 6000000-event ledger, the emitted report has event count 6000000 and exactly matches the reference account summaries, totals, first and last boundaries, timestamps, checksums, and account ordering.
- **C3**: A ledger with duplicate or non-increasing global sequence numbers is rejected without producing a successful report.
- **C4**: Each account checksum is defined over that account's events in ascending timestamp order, with ties resolved by global sequence; account timestamps are not guaranteed to be monotonic in the ledger.

## Subgoals

1. Replace memory-materializing snapshot with streaming processing
2. Sort events per account by timestamp then sequence for correct checksums
3. Maintain sequence validation and all other report fields

## Failure scenario

Preparing a 6M-event ledger and running reconciliation with -Xmx128m causes OutOfMemoryError in EventLog.snapshot, producing no report. A naive streaming fix produces a report with wrong checksums.

## Expected agent pitfalls

- Switching to streaming without sorting events by timestamp, producing wrong checksums
- Sorting all events in memory rather than partitioning, still causing OOM
- Breaking sequence validation while restructuring the reconciliation

## Anti-shortcut measures

- Byte-identical comparison with reference report catches any checksum deviation
- Multiple ledger sizes test both memory efficiency and correctness
- Invalid ledger tests ensure sequence validation is preserved

## Requirement-to-test coverage

| Requirement | Tests |
|---|---|
| C1 | `test_invariant_1_bounded_reconciliation`, `test_counterexample_exact_replay` |
| C2 | `test_invariant_2_exact_reference_report`, `test_counterexample_exact_replay` |
| C3 | `test_invariant_3_rejects_non_increasing_sequences`, `test_edge_cases_and_concurrency` |
| C4 | `test_invariant_4_timestamp_sequence_checksum_order`, `test_edge_cases_and_concurrency`, `test_counterexample_exact_replay` |
