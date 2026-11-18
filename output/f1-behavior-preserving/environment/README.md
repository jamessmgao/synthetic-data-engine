# Pipelinesense

A small, dependency-free toolkit for batch telemetry pipelines. It reads JSON
files of sensor readings and produces aggregate reports, threshold alerts,
daily totals, coverage reports, CSV exports and archive snapshots.

## Usage

Run from the repository root:

    python3 -m pipelinesense report --input data/day1.json
    python3 -m pipelinesense alerts --input data/day1.json --thresholds data/thresholds.json
    python3 -m pipelinesense totals --input data/day1.json
    python3 -m pipelinesense coverage --input data/day1.json --interval 15m
    python3 -m pipelinesense export --input data/day1.json --out readings.csv
    python3 -m pipelinesense snapshot --input data/day1.json --dir /tmp/archive
    python3 -m pipelinesense verify --input data/day1.json --dir /tmp/archive
    python3 -m pipelinesense pipeline --input data/day1.json --dir /tmp/out --thresholds data/thresholds.json

## Input format

A reading file is a JSON list of objects:

    {"sensor": "temp-1", "timestamp": "2024-05-01T09:52:03", "value": 34.2}

Readings are stored in arrival order: sensors are interleaved and files may
end with late-arriving samples, so never assume a reading file is sorted.
