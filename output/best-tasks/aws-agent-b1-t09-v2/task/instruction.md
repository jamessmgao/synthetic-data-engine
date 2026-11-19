# Ledger Reconciliation Resource Optimization

## Symptom

Reconciling a 6 000 000-event ledger under a 128 MiB heap (`-Xmx128m`) fails with an `OutOfMemoryError`; no report is produced. Smaller ledgers reconcile without error.

## Required Guarantees

1. A valid 6 000 000-event ledger reconciliation completes within 75 seconds while the Java process is limited to a 128 MiB heap.
2. For the deterministic 6 000 000-event ledger, the emitted report has event count 6 000 000 and exactly matches the reference account summaries, totals, first and last boundaries, timestamps, checksums, and account ordering.
3. A ledger with duplicate or non-increasing global sequence numbers is rejected without producing a successful report.
4. Each account checksum is defined over that account's events in ascending timestamp order, with ties resolved by global sequence; account timestamps are not guaranteed to be monotonic in the ledger.

## What Must Keep Working

- Valid ledgers of any size up to 6 000 000 events reconcile correctly.
- Report format and content remain byte-identical to the reference for all valid inputs.
- Sequence validation rejects invalid ledgers.
- Account checksums follow the timestamp-then-sequence ordering definition.

## Interface

All commands run from `/app` with classpath `/app`:

- `java Main prepare <output-path> <event-count>` — writes a deterministic ledger file.
- `java Main reconcile <log-path>` — reads a ledger and prints a report to stdout. Exits non-zero on failure; produces no stdout on error.
- `java Main expected <event-count> <account-count>` — prints the reference report for the deterministic ledger.

Heap limit: `-Xmx128m`.

### Report format

First line: `events|<count>`

Subsequent lines, one per account in lexicographic order:
`<account>|<count>|<total>|<firstSeq>|<lastSeq>|<firstTs>|<lastTs>|<checksum>`

### Ledger format

One event per line:
`<sequence>|<timestamp>|<amount>|<account>`

Fields are pipe-separated; sequence, timestamp, and amount are non-negative integers; account names contain lowercase letters, digits, and hyphens.
