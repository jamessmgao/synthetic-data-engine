"""Threshold alerts over interval buckets."""
from . import aggregate as agg
from . import timeutil


def evaluate_alerts(readings, thresholds, interval="1h"):
    seconds = timeutil.interval_seconds(interval)
    alerts = []
    for bucket in agg.aggregate(readings, interval):
        threshold = thresholds.get(bucket.sensor)
        if threshold is None or bucket.maximum <= threshold:
            continue
        trigger = None
        for r in readings:
            if r.sensor != bucket.sensor or r.value <= threshold:
                continue
            if timeutil.bucket_start(r.timestamp, seconds).isoformat() != bucket.start:
                continue
            if trigger is None or r.timestamp < trigger.timestamp:
                trigger = r
        alerts.append({"sensor": bucket.sensor,
                       "bucket_start": bucket.start,
                       "threshold": threshold,
                       "triggered_at": trigger.timestamp.isoformat(),
                       "value": trigger.value})
    alerts.sort(key=lambda a: (a["sensor"], a["bucket_start"]))
    return alerts
