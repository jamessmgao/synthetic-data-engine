"""sde-world: the world author's self-check, run inside the authoring container.

    sde-world check <world_dir>

Runs exactly the pipeline's `world_check` gate (shared/taskformat/world_check.py) on the
world: structure, build, the counterexample's repro.sh exits 1 twice, then 0
twice after world_fix/fix.sh. Prints the result as JSON and exits 0 only if
the world would pass the gate. The author must not finish until it passes.
"""

import argparse
import json
import sys
import tempfile
from pathlib import Path

SDE = Path(__file__).resolve().parent.parent  # /opt/sde in the image, the repo root locally
sys.path[:0] = [str(SDE / "validator"), str(SDE / "generator")]

from shared.taskformat.world_check import run_world_check_job  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sde-world", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    check = sub.add_parser("check", help="run the world_check gate on a world directory")
    check.add_argument("world_dir", type=Path)
    args = parser.parse_args(argv)

    with tempfile.TemporaryDirectory(prefix="sde-world-") as trace:
        result = run_world_check_job(args.world_dir, Path(trace))
    print(json.dumps(result, indent=1))
    if result["outcome"] != "OK":
        print(f"\naq-world: FAILED at {result['failed_check']}:\n{result['feedback']}", file=sys.stderr)
    return 0 if result["outcome"] == "OK" else 1


if __name__ == "__main__":
    sys.exit(main())
