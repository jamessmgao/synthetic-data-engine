# Fix the reading-corruption inconsistency in Pipelinesense

Pipelinesense is a small, dependency-free telemetry pipeline that lives in `/app` and runs from `/app` as:

    python3 -m pipelinesense <command> ...

It ingests JSON files of sensor readings and produces aggregate reports, threshold alerts, daily totals, coverage reports, CSV exports and archive snapshots. The on-call team has reported two problems, both reproducible with the demo data in `data/`:

1. Running `python3 -m pipelinesense pipeline --input data/day1.json --dir /tmp/out --thresholds data/thresholds.json` and then `python3 -m pipelinesense verify --input data/day1.json --dir /tmp/out` fails verification: the archive's raw section no longer matches the source file, and `readings.csv` differs from what `python3 -m pipelinesense export --input data/day1.json --out <file>` produces on the same input.
2. `python3 -m pipelinesense report --input data/day1.json` prints an hourly section that does not reflect the individual hours of the readings, and `python3 -m pipelinesense alerts --input data/day1.json --thresholds data/thresholds.json` reports trigger timestamps that are bucket starts rather than the readings' own timestamps.

Restore the documented behaviour below. Do not change any other observable behaviour: command names, arguments, file names, JSON/CSV formats, ordering rules and exit codes must stay exactly as specified.

## Input data

Reading files are JSON lists of objects:

    {"sensor": "temp-1", "timestamp": "2024-05-01T09:52:03", "value": 34.2}

Files are stored in arrival order: sensors are interleaved and a file may end with late-arriving samples (see `data/day1.json`). Never assume a reading file is sorted. An empty list is valid input: it yields empty report sections, empty alerts/totals/coverage lists, and a CSV containing only the header.

## Required behaviour

### Read-only analysis

No analysis stage may alter ingested reading data. The `pipeline` command loads its input once and runs every stage over that same data set; each stage must therefore see the readings exactly as they appear in the input file. Consequently:

- `export` always writes the readings in input order with their original timestamps and values, no matter which other stages have run.
- the `raw.json` written by `snapshot` is exactly the input file's readings, in input order.
- every artifact written by `pipeline` is identical to what the corresponding standalone command produces on the same input, and `verify` exits 0 after `pipeline`.
- output is deterministic: the same input always produces the same output, however often or in which order commands run.

### Commands and formats

All commands take `--input FILE` and write JSON to stdout unless noted. Exit codes: 0 success, 1 verify mismatch, 2 invalid input (unreadable file, malformed reading, invalid interval such as `90x`) with a message on stderr.

- `report [--interval 1h]` - JSON object `{"summary": [...], "hourly": [...]}`. `summary` holds daily (1d) buckets, `hourly` holds buckets at `--interval` (default `1h`). A bucket is `{"sensor", "start", "count", "min", "max", "mean"}` where `start` is the ISO timestamp of the bucket's start and `mean` is sum/count. A reading belongs to the bucket whose start is the largest bucket start <= its timestamp; bucket starts are aligned to the Unix epoch, so a reading exactly on a boundary belongs to the bucket starting there. Buckets are ordered by `(sensor, start)`. Every reading must be attributed to the bucket containing its own timestamp in both sections.
- `alerts --thresholds FILE [--interval 1h]` (default thresholds file `data/thresholds.json`, relative to the working directory) - JSON list. The thresholds file maps sensor name to numeric limit. For every bucket at `--interval` whose `max` exceeds the sensor's limit, one alert `{"sensor", "bucket_start", "threshold", "triggered_at", "value"}`: `triggered_at`/`value` identify the earliest reading (by timestamp, ties broken by input position) inside that bucket whose value exceeds the limit. Ordered by `(sensor, bucket_start)`.
- `totals` - JSON list of `{"sensor", "day", "samples", "daily_sum", "cumulative"}`: one row per sensor and calendar day (a reading's day is the date part of its timestamp), ordered by `(sensor, day)`; `cumulative` is the running sum of that sensor's `daily_sum` values in day order. This must be correct for unsorted and late-arriving input.
- `coverage [--interval 1h]` - JSON list with one entry per sensor that has readings: `{"sensor", "first", "last", "expected", "present", "missing"}` where `first`/`last` are the sensor's first/last bucket starts, `present` counts the non-empty buckets from `first` to `last` inclusive, `missing` lists the empty bucket starts in chronological order, and `expected = present + len(missing)`.
- `export --out FILE` - CSV with header `sensor,timestamp,value` and one row per reading in input order; timestamps in ISO form, values as Python's `str()` of the parsed float.
- `snapshot --dir DIR [--interval 1h]` - writes into `DIR`: `raw.json` (the readings as loaded), `aggregates.json` (the buckets at `--interval`), and `meta.json` `{"source": basename of the input, "interval": the interval, "readings": number of readings}`.
- `verify --dir DIR [--interval 1h]` - recomputes from `--input` and exits 0 printing a line starting with `OK` only if `raw.json` matches the source readings and `aggregates.json` matches the recomputed buckets; otherwise it exits 1 and prints one line per problem to stderr, naming the offending section (`raw` or `aggregate`).
- `pipeline --dir DIR [--thresholds FILE] [--interval 1h]` - loads the input once, then writes `report.json`, `alerts.json`, `totals.json`, `coverage.json`, `readings.csv` and the snapshot files into `DIR`.

Bucketing, statistics and output formats are already correct for single commands in isolation; keep them that way. After your change, the sequence `pipeline` then `verify` on `data/day1.json` must exit 0.

## Constraints

- Python standard library only; the CLI surface, artifact names and file formats must stay identical.
- The fix must not change any behaviour other than the inconsistencies described above.
