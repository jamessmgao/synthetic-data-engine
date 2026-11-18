"""Command line interface for the pipelinesense toolkit."""
import argparse
import json
import os
import sys

from . import alerts as alerts_mod
from . import archive
from . import coverage
from . import exporter
from . import model
from . import pipeline as pipeline_mod
from . import report
from . import store
from . import totals as totals_mod


def _print_json(doc):
    print(json.dumps(doc, indent=2))


def build_parser():
    parser = argparse.ArgumentParser(
        prog="pipelinesense",
        description="Batch telemetry pipeline toolkit for sensor reading files")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("report", help="aggregate report document")
    p.add_argument("--input", required=True)
    p.add_argument("--interval", default="1h")

    p = sub.add_parser("alerts", help="threshold alerts")
    p.add_argument("--input", required=True)
    p.add_argument("--thresholds", default="data/thresholds.json")
    p.add_argument("--interval", default="1h")

    p = sub.add_parser("totals", help="daily totals with cumulative sums")
    p.add_argument("--input", required=True)

    p = sub.add_parser("coverage", help="bucket coverage report")
    p.add_argument("--input", required=True)
    p.add_argument("--interval", default="1h")

    p = sub.add_parser("export", help="CSV export of the raw readings")
    p.add_argument("--input", required=True)
    p.add_argument("--out", required=True)

    p = sub.add_parser("snapshot", help="archive raw and aggregated data")
    p.add_argument("--input", required=True)
    p.add_argument("--dir", required=True)
    p.add_argument("--interval", default="1h")

    p = sub.add_parser("verify", help="check an archive against its source")
    p.add_argument("--input", required=True)
    p.add_argument("--dir", required=True)
    p.add_argument("--interval", default="1h")

    p = sub.add_parser("pipeline", help="run every stage over one input")
    p.add_argument("--input", required=True)
    p.add_argument("--dir", required=True)
    p.add_argument("--thresholds", default="data/thresholds.json")
    p.add_argument("--interval", default="1h")

    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        if args.command == "report":
            readings = store.load_readings(args.input)
            _print_json(report.build_report(readings, args.interval))
            return 0
        if args.command == "alerts":
            readings = store.load_readings(args.input)
            thresholds = store.load_thresholds(args.thresholds)
            _print_json(alerts_mod.evaluate_alerts(readings, thresholds, args.interval))
            return 0
        if args.command == "totals":
            _print_json(totals_mod.daily_totals(store.load_readings(args.input)))
            return 0
        if args.command == "coverage":
            _print_json(coverage.coverage_gaps(store.load_readings(args.input),
                                               args.interval))
            return 0
        if args.command == "export":
            exporter.export_csv(store.load_readings(args.input), args.out)
            return 0
        if args.command == "snapshot":
            archive.snapshot(store.load_readings(args.input), args.dir,
                             os.path.basename(args.input), args.interval)
            return 0
        if args.command == "verify":
            return archive.verify(args.input, args.dir, args.interval)
        if args.command == "pipeline":
            result = pipeline_mod.run_pipeline(args.input, args.dir,
                                               args.thresholds, args.interval)
            _print_json(result)
            return 0
    except (OSError, model.ReadingError, ValueError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    return 2
