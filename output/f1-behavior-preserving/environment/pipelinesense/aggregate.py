"""Fixed-interval bucket aggregation over readings."""
from dataclasses import dataclass

from . import timeutil


@dataclass
class Bucket:
    sensor: str
    start: str
    count: int
    minimum: float
    maximum: float
    mean: float


def aggregate(readings, interval="1h"):
    seconds = timeutil.interval_seconds(interval)
    for r in readings:
        r.timestamp = timeutil.bucket_start(r.timestamp, seconds)
    readings.sort(key=lambda r: (r.sensor, r.timestamp))
    grouped = {}
    for r in readings:
        grouped.setdefault((r.sensor, r.timestamp), []).append(r.value)
    buckets = []
    for (sensor, start), values in grouped.items():
        buckets.append(Bucket(sensor=sensor, start=start.isoformat(),
                              count=len(values), minimum=min(values),
                              maximum=max(values),
                              mean=sum(values) / len(values)))
    buckets.sort(key=lambda b: (b.sensor, b.start))
    return buckets


def bucket_to_dict(bucket):
    return {"sensor": bucket.sensor, "start": bucket.start,
            "count": bucket.count, "min": bucket.minimum,
            "max": bucket.maximum, "mean": bucket.mean}
