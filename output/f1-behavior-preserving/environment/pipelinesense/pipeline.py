"""End-to-end pipeline over a single input file."""
import json
import os

from . import alerts as alerts_mod
from . import archive
from . import coverage
from . import exporter
from . import report
from . import store
from . import totals as totals_mod


def _write_json(path, doc):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
        fh.write("\n")


def run_pipeline(input_path, out_dir, thresholds_path, interval="1h"):
    os.makedirs(out_dir, exist_ok=True)
    readings = store.load_readings(input_path)
    thresholds = store.load_thresholds(thresholds_path) if thresholds_path else {}

    _write_json(os.path.join(out_dir, "report.json"),
                report.build_report(readings, interval))
    _write_json(os.path.join(out_dir, "alerts.json"),
                alerts_mod.evaluate_alerts(readings, thresholds, interval))
    _write_json(os.path.join(out_dir, "totals.json"),
                totals_mod.daily_totals(readings))
    _write_json(os.path.join(out_dir, "coverage.json"),
                coverage.coverage_gaps(readings, interval))
    exporter.export_csv(readings, os.path.join(out_dir, "readings.csv"))
    archive.snapshot(readings, out_dir, os.path.basename(input_path), interval)
    return {"readings": len(readings)}
