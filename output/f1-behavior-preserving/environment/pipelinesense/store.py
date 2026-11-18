"""Reading and threshold file access."""
import json

from . import model


def load_readings(path):
    with open(path, "r", encoding="utf-8") as fh:
        payload = json.load(fh)
    if not isinstance(payload, list):
        raise model.ReadingError("input file must contain a JSON list of readings")
    return [model.from_dict(item) for item in payload]


def dump_readings(readings, path):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump([model.to_dict(r) for r in readings], fh, indent=2)
        fh.write("\n")


def load_thresholds(path):
    with open(path, "r", encoding="utf-8") as fh:
        payload = json.load(fh)
    if not isinstance(payload, dict):
        raise model.ReadingError("thresholds file must contain a JSON object")
    return {str(key): float(value) for key, value in payload.items()}
