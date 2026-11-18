"""Archive snapshots and consistency verification."""
import json
import os
import sys

from . import aggregate as agg
from . import model
from . import store


def snapshot(readings, out_dir, source_name, interval="1h"):
    os.makedirs(out_dir, exist_ok=True)
    store.dump_readings(readings, os.path.join(out_dir, "raw.json"))
    buckets = agg.aggregate(readings, interval)
    with open(os.path.join(out_dir, "aggregates.json"), "w", encoding="utf-8") as fh:
        json.dump([agg.bucket_to_dict(b) for b in buckets], fh, indent=2)
        fh.write("\n")
    meta = {"source": source_name, "interval": interval,
            "readings": len(readings)}
    with open(os.path.join(out_dir, "meta.json"), "w", encoding="utf-8") as fh:
        json.dump(meta, fh, indent=2)
        fh.write("\n")


def verify(input_path, out_dir, interval="1h"):
    problems = []
    fresh = store.load_readings(input_path)
    try:
        archived = store.load_readings(os.path.join(out_dir, "raw.json"))
    except OSError:
        print("error: raw.json missing from archive", file=sys.stderr)
        return 1
    if [model.to_dict(r) for r in archived] != [model.to_dict(r) for r in fresh]:
        problems.append("raw section does not match source")
    with open(os.path.join(out_dir, "aggregates.json"), "r", encoding="utf-8") as fh:
        archived_buckets = json.load(fh)
    expected_buckets = [agg.bucket_to_dict(b) for b in agg.aggregate(fresh, interval)]
    if archived_buckets != expected_buckets:
        problems.append("aggregate section does not match recomputation")
    if problems:
        for problem in problems:
            print("error: %s" % problem, file=sys.stderr)
        return 1
    print("OK: archive matches source")
    return 0
