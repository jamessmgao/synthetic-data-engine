"""Aggregated report document."""
from . import aggregate as agg


def build_report(readings, interval="1h"):
    daily = agg.aggregate(readings, "1d")
    hourly = agg.aggregate(readings, interval)
    return {
        "summary": [agg.bucket_to_dict(b) for b in daily],
        "hourly": [agg.bucket_to_dict(b) for b in hourly],
    }
