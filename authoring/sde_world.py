"""sde-world: the world author's self-check, run inside the authoring container.

    sde-world check <world_dir>

Runs exactly the pipeline's `world_check` gate (shared/taskformat/world_check.py) on the
world: structure, build, the counterexample's repro.sh exits 1 twice, then 0
twice after world_fix/fix.sh. Prints the result as JSON and exits 0 only if
the world would pass the gate. The author must not finish until it passes.

The result is also saved to $SDE_ARTIFACTS_DIR/world_check.json (default
/logs/artifacts, if it exists): the pipeline reads it as the evidence that the
author self-checked, and runs the check itself when it's missing.
"""

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

SDE = Path(__file__).resolve().parent.parent  # /opt/sde in the image, the repo root locally
sys.path[:0] = [str(SDE / "validator"), str(SDE / "generator")]

from shared.taskformat.world_check import run_world_check_job, world_stamp  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sde-world", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    check = sub.add_parser("check", help="run the world_check gate on a world directory")
    check.add_argument("world_dir", type=Path)
    args = parser.parse_args(argv)

    with tempfile.TemporaryDirectory(prefix="sde-world-") as trace:
        result = run_world_check_job(args.world_dir, Path(trace))
    # The world this result is for: the pipeline uses the saved check only if it still matches.
    result = {**result, "stamp": world_stamp(args.world_dir)}
    text = json.dumps(result, indent=1)
    artifacts = Path(os.environ.get("SDE_ARTIFACTS_DIR", "/logs/artifacts"))
    if artifacts.is_dir():
        (artifacts / "world_check.json").write_text(text + "\n")
    print(text)
    if result["outcome"] != "OK":
        print(f"\naq-world: FAILED at {result['failed_check']}:\n{result['feedback']}", file=sys.stderr)
    for line in hardness_warnings((result.get("result") or {}).get("hardness")):
        print(line, file=sys.stderr)
    return 0 if result["outcome"] == "OK" else 1


def hardness_warnings(hardness: dict | None) -> list[str]:
    """One WARNING per failed hardness check, each with what to change. In warn mode they
    don't fail the check, but a world that misses them makes an easier task."""
    if not hardness or not (hardness.get("failed") or hardness.get("warnings")):
        return []
    details = hardness.get("details") or {}
    named = [f"WARNING: {hit['file']}: {hit['function']} ({hit['where']}: \"{hit['text']}\") names its own limit "
             f"({', '.join(hit['matched'])}), and fix.sh changes it: an agent reads that and has the bug. Name and "
             "comment the code by what it is for, never by the limit that breaks it"
             for hit in details.get("limit_named") or []]
    if not hardness.get("failed"):
        return ["", f"sde-world: {len(named)} naming warning(s) (they never fail the world):"] + named
    files = details.get("fix_files")
    say = {
        "hardness_files": f"missing {details.get('missing')}: write repro/symptom_check.sh (the visible symptom) "
                          "and world_fix/wrong_fix.sh (the plausible wrong fix), see world.md",
        "symptom_visible": "repro/symptom_check.sh does not fail on the world: make it reproduce the visible "
                           "symptom users report (exit 1 on the world)",
        "symptom_gone_with_fix": "repro/symptom_check.sh still fails after fix.sh: the real fix must remove the "
                                 "visible symptom too",
        "fix_not_local": f"fix.sh changes {len(files) if files is not None else '?'} file(s) {files or ''} and "
                         "there is no working dependent_check.sh: make the fix span 2+ modules, or add a module "
                         "that depends on the current behavior and a repro/dependent_check.sh the local patch breaks",
        "dependent_holds": "repro/dependent_check.sh fails on the unfixed world: the dependent must work today",
        "dependent_holds_with_fix": "repro/dependent_check.sh fails after fix.sh: the real fix must keep the "
                                    "dependent working",
        "dependent_breaks_with_wrong_fix": "repro/dependent_check.sh still passes after wrong_fix.sh: the local "
                                           "patch must break the dependent",
        "wrong_fix_passes_symptom": "the plausible wrong fix does not pass symptom_check.sh, so it is not tempting: "
                                    "make it fix the visible symptom",
        "wrong_fix_fails_repro": "repro.sh passes after wrong_fix.sh, so the wrong fix is actually right: make it "
                                 "fail on the deep schedule (another crash point, a restart, a concurrent worker)",
    }
    mode = hardness.get("mode", "warn")
    head = ["", f"sde-world: {len(hardness['failed'])} hardness warning(s) (mode {mode}"
                + (": the world still passes" if mode == "warn" else "") + "). Fix every WARNING before finishing "
                "if time allows:"]
    return head + [f"WARNING: {say.get(name, name)}" for name in hardness["failed"]] + named


if __name__ == "__main__":
    sys.exit(main())
