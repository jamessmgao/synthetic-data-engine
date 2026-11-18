"""Per-sensor daily totals with a running cumulative sum."""
from . import aggregate as agg


def daily_totals(readings):
    agg.aggregate(readings, "1d")
    rows = []
    index = {}
    for r in readings:
        day = r.timestamp.date().isoformat()
        key = (r.sensor, day)
        if key not in index:
            row = {"sensor": r.sensor, "day": day, "samples": 0,
                   "daily_sum": 0.0, "cumulative": 0.0}
            index[key] = row
            rows.append(row)
        index[key]["samples"] += 1
        index[key]["daily_sum"] += r.value
    cumulative = {}
    for row in rows:
        cumulative[row["sensor"]] = cumulative.get(row["sensor"], 0.0) + row["daily_sum"]
        row["cumulative"] = cumulative[row["sensor"]]
    return rows
