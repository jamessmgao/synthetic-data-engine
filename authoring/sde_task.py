"""sde-task: the task author's self-check, run inside the authoring container.

    sde-task schema
    sde-task check <draft.json> [--world <world_dir>]
    sde-task run <draft.json | bundle_dir> --as oracle|nop|wrong|oracle,nop,wrong|all [--world <world_dir>]

`schema` prints the TaskDraft JSON schema: every field draft.json must have.

`check` runs every deterministic check the pipeline runs on a TaskDraft (schema,
paths, Dockerfile, syntax, stdlib + pytest only test imports, coverage, leaks...),
plus one tie to the world: the wrong fix must be the world's plausible wrong fix,
so wrong_fix_script has to quote world.json's `plausible_wrong_fix` in a comment
line `# plausible_wrong_fix: <text>`. It then materializes the bundle.
Prints {ok, errors, bundle_dir}; exit 0 iff ok.

`run` is a small Harbor through the host Docker socket: build environment/, start a
container capped like the evaluation (1 CPU / 2 GB), copy solution/ in and run the
role's script as the `agent` user (oracle: solve.sh, wrong: wrong_fix.sh, nop:
nothing), copy tests/ in and run test.sh as root, read the reward and the per-test
results. nop and wrong must fail BECAUSE of the bug: a failure from a setup error
(ImportError, a missing file or module, no verdict at all) doesn't count, and is
reported as such. Prints {as, reward, expected, ok, tests, setup_errors, output_tail,
image_reused} for one role, {ok, runs: [...]} for several; exit 0 iff ok.

environment/ is built once: several roles run in fresh containers from one image, and
inside an authoring trial ($SDE_DOCKER_LABEL set) the image is tagged by the content
of environment/ and kept, so a later `sde-task run` on the same environment skips the
build (the harness prunes labeled images after the trial).

Everything built or started is labeled with $SDE_DOCKER_LABEL and removed after.
Each check / run result is also saved to $SDE_ARTIFACTS_DIR/sde_task/<check|run-ROLE>.json
(default /logs/artifacts, if it exists): the pipeline reads those as the evidence
that the author self-checked. Each carries a `stamp`, the draft's content hash
(materialize.content_sha256): a saved result counts only for the exact final draft, and
validate may adopt the runs instead of repeating them (SDE_ADOPT_AUTHOR_CHECKS). A run on a
bundle dir has no draft, so no stamp: the pipeline runs it again.
"""

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

SDE = Path(__file__).resolve().parent.parent  # /opt/sde in the image, the repo root locally
if not __package__:  # run by path (bin/sde-task): the root goes on the import path, in place of this directory
    _here = Path(__file__).resolve().parent
    sys.path[:] = [str(_here.parent)] + [p for p in sys.path if p != str(_here)]

from shared.taskformat.schema import TaskDraft  # noqa: E402
from shared.taskformat.task_check import EXPECTED, check_draft, run_roles  # noqa: E402

DEFAULT_WORLD = Path("/input/world")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sde-task", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("schema", help="print the TaskDraft JSON schema draft.json must match")
    check = sub.add_parser("check", help="validate a draft and materialize it")
    check.add_argument("draft", type=Path)
    check.add_argument("--world", type=Path, default=None)
    run = sub.add_parser("run", help="run the verifier after oracle / nop / wrong fix")
    run.add_argument("target", type=Path, help="draft.json or a materialized bundle dir")
    run.add_argument("--as", dest="role", type=_roles, required=True,
                     help="oracle, nop, wrong, a comma list (oracle,nop,wrong) or all: one image for every role")
    run.add_argument("--world", type=Path, default=None)
    args = parser.parse_args(argv)
    if args.cmd == "schema":
        print(json.dumps(TaskDraft.model_json_schema(), indent=1))
        return 0
    world = args.world or (DEFAULT_WORLD if DEFAULT_WORLD.is_dir() else None)

    with tempfile.TemporaryDirectory(prefix="sde-task-") as tmp:
        if args.cmd == "check":
            result = check_draft(args.draft, world, Path(tmp))
            save("check.json", json.dumps(result, indent=1))
            print(json.dumps(result, indent=1))
            return 0 if result["ok"] else 1
        bundle = args.target
        stamp = None  # a run on a bundle dir can't be tied to a draft: the pipeline re-runs it
        if not bundle.is_dir():
            checked = check_draft(args.target, world, Path(tmp))
            stamp = checked.get("stamp")
            if not checked["ok"]:
                runs = [{"as": r, "ok": False, "setup_errors": ["the draft fails `sde-task check`"], **checked}
                        for r in args.role]
            bundle = Path(checked["bundle_dir"]) if checked["ok"] else None
        if bundle is not None:
            # The full self-check (all three roles) also runs the oracle twice: the flake check.
            runs = run_roles(bundle, args.role, repeat_oracle=set(args.role) == set(EXPECTED))
        for r in runs:
            r["stamp"] = stamp
            save(f"run-{r['as']}.json", json.dumps(r, indent=1))
        result = runs[0] if len(runs) == 1 else {"ok": all(r["ok"] for r in runs), "runs": runs}
        print(json.dumps(result, indent=1))
    return 0 if result["ok"] else 1


def _roles(value: str) -> list[str]:
    roles = list(EXPECTED) if value == "all" else [r.strip() for r in value.split(",") if r.strip()]
    bad = [r for r in roles if r not in EXPECTED]
    if bad or not roles:
        raise argparse.ArgumentTypeError(f"roles are {', '.join(EXPECTED)} or all, not {value!r}")
    return list(dict.fromkeys(roles))


def save(name: str, text: str) -> None:
    """Keep the latest result where the pipeline looks for self-check evidence."""
    artifacts = Path(os.environ.get("SDE_ARTIFACTS_DIR", "/logs/artifacts"))
    if artifacts.is_dir():
        (artifacts / "sde_task").mkdir(exist_ok=True)
        (artifacts / "sde_task" / name).write_text(text + "\n")


if __name__ == "__main__":
    sys.exit(main())
