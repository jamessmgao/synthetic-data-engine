# Fix reading-corruption inconsistency in the Pipelinesense pipeline

Pipelinesense is a dependency-free telemetry pipeline CLI. A shared aggregation stage mutates the caller's reading data (aligning timestamps in place and re-sorting the caller's list). Because the pipeline command loads its input once and passes the same data to every stage, later stages see corrupted data: CSV exports and archive raw sections no longer match the source, the report's hourly section collapses all readings of a day into the 00:00 bucket, and alerts report bucket starts instead of the triggering readings' own timestamps. A correct fix makes the shared utility read-only and restores each module's documented attribution semantics, while keeping totals working even though daily_totals currently relies on the aggregation side effect for its ordering (a naive purity fix silently regresses totals on unsorted or late-arriving input).

## Scenario

Shared utility module used by many dependent modules has a defect; the fix must not change any other observable behavior.

## Root cause (hidden from the agent)

pipelinesense/aggregate.py rewrites each Reading.timestamp in place to the bucket start and sorts the caller's list; pipelinesense/totals.py depends on that side effect for chronological ordering, so the fix must also make daily_totals self-sufficient.

## Correctness constraints

- **C1**: Analysis stages never modify ingested readings: every pipeline artifact equals the corresponding standalone command output on the same input; snapshot raw.json equals the source readings in input order; export writes the readings in input order with original timestamps and values; results are deterministic and repeatable across runs.
- **C2**: report attributes every reading to the bucket containing its own timestamp (daily summary section plus hourly section at --interval), with documented bucket fields and (sensor, start) ordering; a reading exactly on a bucket boundary belongs to the bucket starting there.
- **C3**: alerts reports triggered_at/value of the earliest reading (by timestamp, ties by input position) inside each bucket whose max exceeds the sensor's threshold; triggered_at must be that reading's own timestamp, not the bucket start (except when the triggering reading happens to lie exactly on its bucket's start boundary).
- **C4**: totals stays grouped per sensor and calendar day, ordered by (sensor, day), with correct running cumulative sums, including for unsorted and late-arriving input files.
- **C5**: All documented formats and behaviors that are already correct stay unchanged: coverage report contents, CSV/JSON formats, snapshot meta.json fields, empty-input behavior, and exit code 2 with a stderr message for invalid input.
- **C6**: verify exits 0 with an OK message on an intact archive and exits 1, naming the offending section on stderr, when the archive's raw or aggregate section does not match the source.

## Subgoals

1. Make the shared aggregation stage treat its input as read-only without changing its documented bucketing, statistics, or output ordering.
2. Restore per-reading attribution in report and alerts once the data is no longer rewritten in place.
3. Make daily totals independent of the aggregation stage's former side effects so unsorted and late-arriving input still yields ordered rows and correct cumulative sums.

## Failure scenario

Reproducible with the demo data: (1) pipeline then verify fails because raw.json and readings.csv no longer match the source; (2) report's hourly section collapses each day's readings into a single 00:00 bucket and alerts prints bucket starts as trigger timestamps.

## Expected agent pitfalls

- A naive purity fix in the shared utility silently regresses daily totals, which currently relies on the utility's in-place alignment and sort for its ordering; the regression only shows on unsorted or late-arriving input.
- Copying inputs at a single call site (e.g. only in report) does not help because the pipeline command passes one loaded data set to every stage.
- Reordering pipeline stages cannot hide the defect because report and alerts are wrong even standalone.

## Anti-shortcut measures

- Tests generate their own deterministic fixtures (seeded, shuffled, with late samples) and derive expected outputs from a spec-level reimplementation, so hard-coded outputs cannot pass.
- A tampering test corrupts an archive and requires verify to reject it, so weakening verify cannot pass.
- Pipeline artifacts are compared both against standalone command outputs and against the specification.

## Requirement-to-test coverage

| Requirement | Tests |
|---|---|
| C1 | `test_pipeline_artifacts_match_standalone`, `test_pipeline_verify_ok`, `test_pipeline_is_repeatable`, `test_standalone_export_matches_source`, `test_snapshot_matches_source` |
| C2 | `test_report_matches_spec`, `test_report_generated_fixture`, `test_bucket_boundary_attribution`, `test_pipeline_artifacts_match_standalone` |
| C3 | `test_alerts_trigger_attribution`, `test_alerts_generated_fixture`, `test_pipeline_artifacts_match_standalone` |
| C4 | `test_totals_late_mix`, `test_totals_generated_fixture`, `test_pipeline_artifacts_match_standalone` |
| C5 | `test_coverage_matches_spec`, `test_standalone_export_matches_source`, `test_snapshot_matches_source`, `test_empty_input`, `test_cli_error_handling` |
| C6 | `test_pipeline_verify_ok`, `test_verify_detects_tampering`, `test_empty_input` |
