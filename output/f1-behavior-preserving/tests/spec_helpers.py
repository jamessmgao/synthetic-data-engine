"""Specification-level reference behaviour for the Pipelinesense CLI.

The regression suite derives its expected outputs from these helpers, which
implement the documented contracts independently of the application code.
"""

import json
import math
import os
import random
import subprocess
from datetime import datetime, timedelta

APP_DIR = "/app"
EPOCH = datetime(1970, 1, 1)


def run_cli(*args, timeout=60):
    cmd = ["python3", "-m", "pipelinesense"] + [str(a) for a in args]
    return subprocess.run(cmd, cwd=APP_DIR, capture_output=True,
                          text=True, timeout=timeout)


def load_json_file(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def write_json_file(path, doc):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
        fh.write("\n")


LATE_MIX = [
    {"sensor": "alpha", "timestamp": "2024-06-01T09:57:30", "value": 21.5},
    {"sensor": "beta", "timestamp": "2024-06-01T10:00:00", "value": 5.25},
    {"sensor": "alpha", "timestamp": "2024-06-01T10:03:41", "value": 22.0},
    {"sensor": "alpha", "timestamp": "2024-06-01T10:59:59", "value": 23.5},
    {"sensor": "beta", "timestamp": "2024-06-01T11:02:10", "value": 5.5},
    {"sensor": "alpha", "timestamp": "2024-06-01T11:20:05", "value": 24.25},
    {"sensor": "gamma", "timestamp": "2024-06-01T11:45:00", "value": 101.0},
    {"sensor": "beta", "timestamp": "2024-06-01T23:58:00", "value": 4.75},
    {"sensor": "alpha", "timestamp": "2024-06-02T00:30:00", "value": 20.75},
    {"sensor": "gamma", "timestamp": "2024-06-02T08:15:30", "value": 99.5},
    {"sensor": "alpha", "timestamp": "2024-06-02T09:05:12", "value": 21.25},
    {"sensor": "beta", "timestamp": "2024-06-02T09:44:48", "value": 6.0},
    {"sensor": "alpha", "timestamp": "2024-06-01T10:07:19", "value": 30.5},
    {"sensor": "gamma", "timestamp": "2024-06-02T07:59:59", "value": 100.25},
    {"sensor": "alpha", "timestamp": "2024-06-01T09:03:22", "value": 31.75},
    {"sensor": "alpha", "timestamp": "2024-05-31T22:10:00", "value": 19.5},
]

LATE_MIX_THRESHOLDS = {"alpha": 30.0, "gamma": 100.0}


def write_late_mix(dirname):
    dirname = str(dirname)
    os.makedirs(dirname, exist_ok=True)
    input_path = os.path.join(dirname, "late_mix.json")
    thresholds_path = os.path.join(dirname, "thresholds.json")
    write_json_file(input_path, LATE_MIX)
    write_json_file(thresholds_path, LATE_MIX_THRESHOLDS)
    return input_path, thresholds_path


def write_generated(dirname):
    dirname = str(dirname)
    os.makedirs(dirname, exist_ok=True)
    rng = random.Random(20240617)
    sensors = ["flow-a", "flow-b", "press-c"]
    records = []
    base = datetime(2024, 7, 8, 0, 0, 0)
    for hour in range(48):
        for sensor in sensors:
            if rng.random() < 0.85:
                ts = base + timedelta(hours=hour, minutes=rng.randrange(60),
                                       seconds=rng.randrange(60))
                records.append({"sensor": sensor, "timestamp": ts.isoformat(),
                                "value": round(40.0 + 20.0 * rng.random(), 1)})
    for _ in range(12):
        hour = rng.randrange(48)
        sensor = sensors[rng.randrange(3)]
        ts = base + timedelta(hours=hour, minutes=rng.randrange(60),
                              seconds=rng.randrange(60))
        records.append({"sensor": sensor, "timestamp": ts.isoformat(),
                        "value": round(85.0 + 10.0 * rng.random(), 1)})
    rng.shuffle(records)
    input_path = os.path.join(dirname, "generated.json")
    thresholds_path = os.path.join(dirname, "thresholds.json")
    write_json_file(input_path, records)
    write_json_file(thresholds_path,
                    {"flow-a": 80.0, "flow-b": 80.0, "press-c": 80.0})
    return input_path, thresholds_path


def interval_seconds(interval):
    text = str(interval)
    units = {"m": 60, "h": 3600, "d": 86400}
    if len(text) < 2 or text[-1] not in units or not text[:-1].isdigit():
        raise ValueError("invalid interval: %r" % (interval,))
    count = int(text[:-1])
    if count <= 0:
        raise ValueError("invalid interval: %r" % (interval,))
    return count * units[text[-1]]


def bucket_start(ts, seconds):
    delta = ts - EPOCH
    return EPOCH + timedelta(seconds=(delta // timedelta(seconds=seconds)) * seconds)


def expected_aggregate(records, interval="1h"):
    seconds = interval_seconds(interval)
    grouped = {}
    for rec in records:
        ts = datetime.fromisoformat(rec["timestamp"])
        start = bucket_start(ts, seconds)
        grouped.setdefault((rec["sensor"], start), []).append(float(rec["value"]))
    buckets = []
    for (sensor, start), values in grouped.items():
        buckets.append({"sensor": sensor, "start": start.isoformat(),
                        "count": len(values), "min": min(values),
                        "max": max(values),
                        "mean": sum(values) / len(values)})
    buckets.sort(key=lambda b: (b["sensor"], b["start"]))
    return buckets


def expected_report(records, interval="1h"):
    return {"summary": expected_aggregate(records, "1d"),
            "hourly": expected_aggregate(records, interval)}


def expected_alerts(records, thresholds, interval="1h"):
    seconds = interval_seconds(interval)
    limits = {str(k): float(v) for k, v in thresholds.items()}
    alerts = []
    for bucket in expected_aggregate(records, interval):
        limit = limits.get(bucket["sensor"])
        if limit is None or bucket["max"] <= limit:
            continue
        candidates = []
        for idx, rec in enumerate(records):
            if rec["sensor"] != bucket["sensor"]:
                continue
            if float(rec["value"]) <= limit:
                continue
            ts = datetime.fromisoformat(rec["timestamp"])
            if bucket_start(ts, seconds) != datetime.fromisoformat(bucket["start"]):
                continue
            candidates.append((ts, idx, rec))
        ts, _, rec = min(candidates, key=lambda item: (item[0], item[1]))
        alerts.append({"sensor": bucket["sensor"],
                       "bucket_start": bucket["start"],
                       "threshold": limit,
                       "triggered_at": rec["timestamp"],
                       "value": float(rec["value"])})
    alerts.sort(key=lambda a: (a["sensor"], a["bucket_start"]))
    return alerts


def expected_totals(records):
    rows = []
    index = {}
    for rec in records:
        day = datetime.fromisoformat(rec["timestamp"]).date().isoformat()
        key = (rec["sensor"], day)
        if key not in index:
            row = {"sensor": rec["sensor"], "day": day, "samples": 0,
                   "daily_sum": 0.0, "cumulative": 0.0}
            index[key] = row
            rows.append(row)
        index[key]["samples"] += 1
        index[key]["daily_sum"] += float(rec["value"])
    rows.sort(key=lambda row: (row["sensor"], row["day"]))
    cumulative = {}
    for row in rows:
        cumulative[row["sensor"]] = (cumulative.get(row["sensor"], 0.0)
                                     + row["daily_sum"])
        row["cumulative"] = cumulative[row["sensor"]]
    return rows


def expected_coverage(records, interval="1h"):
    seconds = interval_seconds(interval)
    grouped = {}
    for bucket in expected_aggregate(records, interval):
        grouped.setdefault(bucket["sensor"], []).append(bucket)
    reports = []
    for sensor in sorted(grouped):
        buckets = grouped[sensor]
        first = datetime.fromisoformat(buckets[0]["start"])
        last = datetime.fromisoformat(buckets[-1]["start"])
        present = {b["start"] for b in buckets}
        missing = []
        cursor = first
        while cursor <= last:
            if cursor.isoformat() not in present:
                missing.append(cursor.isoformat())
            cursor = cursor + timedelta(seconds=seconds)
        reports.append({"sensor": sensor, "first": first.isoformat(),
                        "last": last.isoformat(),
                        "expected": len(present) + len(missing),
                        "present": len(present), "missing": missing})
    return reports


def expected_csv(records):
    lines = ["sensor,timestamp,value"]
    for rec in records:
        lines.append("%s,%s,%s" % (rec["sensor"], rec["timestamp"],
                                    str(float(rec["value"]))))
    return "\n".join(lines) + "\n"


def assert_docs_equal(actual, expected, label="document"):
    _compare(actual, expected, label)


def _compare(actual, expected, path):
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            raise AssertionError("%s: expected object, got %r" % (path, actual))
        if set(actual) != set(expected):
            raise AssertionError("%s: keys %r vs %r"
                                 % (path, sorted(actual), sorted(expected)))
        for key in expected:
            _compare(actual[key], expected[key], "%s.%s" % (path, key))
        return
    if isinstance(expected, list):
        if not isinstance(actual, list):
            raise AssertionError("%s: expected list, got %r" % (path, actual))
        if len(actual) != len(expected):
            raise AssertionError("%s: %d items, expected %d"
                                 % (path, len(actual), len(expected)))
        for pos, (item, want) in enumerate(zip(actual, expected)):
            _compare(item, want, "%s[%d]" % (path, pos))
        return
    if isinstance(expected, bool) or isinstance(actual, bool):
        if actual is not expected:
            raise AssertionError("%s: %r != %r" % (path, actual, expected))
        return
    if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
        if not math.isclose(float(actual), float(expected),
                            rel_tol=1e-9, abs_tol=1e-12):
            raise AssertionError("%s: %r != %r" % (path, actual, expected))
        return
    if actual != expected:
        raise AssertionError("%s: %r != %r" % (path, actual, expected))
