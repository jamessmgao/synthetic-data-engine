"""Bucket coverage report."""
from datetime import datetime, timedelta

from . import aggregate as agg
from . import timeutil


def coverage_gaps(readings, interval="1h"):
    seconds = timeutil.interval_seconds(interval)
    grouped = {}
    for bucket in agg.aggregate(readings, interval):
        grouped.setdefault(bucket.sensor, []).append(bucket)
    reports = []
    for sensor in sorted(grouped):
        buckets = grouped[sensor]
        first = datetime.fromisoformat(buckets[0].start)
        last = datetime.fromisoformat(buckets[-1].start)
        present = {b.start for b in buckets}
        missing = []
        cursor = first
        while cursor <= last:
            if cursor.isoformat() not in present:
                missing.append(cursor.isoformat())
            cursor = cursor + timedelta(seconds=seconds)
        reports.append({"sensor": sensor,
                        "first": first.isoformat(),
                        "last": last.isoformat(),
                        "expected": len(present) + len(missing),
                        "present": len(present),
                        "missing": missing})
    return reports
