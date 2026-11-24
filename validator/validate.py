"""
Structural check of one Harbor (Terminal-Bench 2) task directory: the first and
cheapest gate a bundle meets, run before any image is built.

    layout    task.toml and a non-blank instruction.md; environment/, solution/, tests/
    schema    task.toml parses, and harbor's own TaskConfig accepts it
    policy    what this pipeline requires and harbor leaves open: a [task] table,
              [metadata].difficulty, timeouts above zero, an agent timeout of an hour or more
    image     environment/ can be built: a Dockerfile with a FROM line, a compose
              file, or [environment].docker_image
    scripts   solution/solve.{sh,bat}; tests/test.{sh,bat}, which names a reward file
    harbor    Task.is_valid_dir, asked last and only when nothing above failed

Errors fail the task. Warnings (no schema_version, a blank description, no Python
test file, no canary string) are reported and never fail it.

The schema is harbor's, not a copy: the models come from the installed package
(generator/requirements.txt), so a harbor upgrade moves this check with it.

Usage: python -m validator.validate <task_dir>      exit 0 = passed, 1 = errors
"""

import argparse
import re
import sys
import tomllib
from collections.abc import Iterator
from pathlib import Path

from harbor.models.difficulty import Difficulty
from harbor.models.task.config import TaskConfig
from harbor.models.task.paths import TaskPaths
from harbor.models.task.task import Task
from pydantic import ValidationError

ERROR, WARNING = "error", "warning"
Finding = tuple[str, str]  # (ERROR or WARNING, message)

MIN_AGENT_TIMEOUT_S = 3600  # an agent run must not fail only because the clock ran out
DIFFICULTIES = tuple(d.value for d in Difficulty)
COMPOSE_NAMES = ("docker-compose.yaml", "docker-compose.yml")
REWARD_NAMES = ("reward.txt", "reward.json", "rewards.json")
FROM_LINE = re.compile(r"^\s*FROM\s", re.IGNORECASE | re.MULTILINE)


def _text(path: Path) -> str:
    try:
        return path.read_text(errors="replace")
    except OSError:
        return ""


def read_config(paths: TaskPaths) -> tuple[dict, TaskConfig | None, list[str]]:
    """task.toml as (raw tables, harbor model, schema errors); the model is None when there are errors."""
    try:
        raw = tomllib.loads(paths.config_path.read_text())
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        return {}, None, [f"task.toml is not valid TOML: {e}"]
    try:
        return raw, TaskConfig.model_validate(raw), []
    except ValidationError as e:
        return raw, None, [f"task.toml fails harbor's schema at {'.'.join(map(str, err['loc'])) or 'the top level'}: {err['msg']}"
                           for err in e.errors()]


def check_layout(paths: TaskPaths) -> Iterator[Finding]:
    for file in (paths.config_path, paths.instruction_path):
        if not file.is_file():
            yield ERROR, f"{file.name} is not in the task directory"
    for directory in (paths.environment_dir, paths.solution_dir, paths.tests_dir):
        if not directory.is_dir():
            yield ERROR, f"{directory.name}/ is not in the task directory"
    if paths.instruction_path.is_file() and not _text(paths.instruction_path).strip():
        yield ERROR, "instruction.md is blank"


def check_policy(raw: dict, config: TaskConfig | None) -> Iterator[Finding]:
    if "schema_version" not in raw and "version" not in raw:
        yield WARNING, "task.toml pins no schema_version, so harbor assumes its current one"
    if config is None:
        return
    if config.task is None:
        yield ERROR, "task.toml has no [task] table; it carries the task's org/name"
    elif not config.task.description.strip():
        yield WARNING, "[task].description is blank"

    difficulty = config.metadata.get("difficulty")
    if difficulty not in DIFFICULTIES:
        yield ERROR, f"[metadata].difficulty is {difficulty!r}; it must be one of: {', '.join(DIFFICULTIES)}"

    for key, seconds in (("[verifier].timeout_sec", config.verifier.timeout_sec),
                         ("[environment].build_timeout_sec", config.environment.build_timeout_sec)):
        if not seconds > 0:
            yield ERROR, f"{key} is {seconds:g}; a timeout must be above zero"
    agent = config.agent.timeout_sec
    if agent is None:
        yield ERROR, f"[agent].timeout_sec is not set; the agent gets {MIN_AGENT_TIMEOUT_S} s or more"
    elif not agent >= MIN_AGENT_TIMEOUT_S:
        yield ERROR, f"[agent].timeout_sec is {agent:g}; the agent gets {MIN_AGENT_TIMEOUT_S} s or more"


def check_image(paths: TaskPaths, raw: dict) -> Iterator[Finding]:
    build_dir = paths.environment_dir
    if not build_dir.is_dir():
        return  # check_layout has reported it
    recipe = build_dir / "Dockerfile"
    if recipe.is_file():
        if not FROM_LINE.search(_text(recipe)):
            yield ERROR, "environment/Dockerfile has no FROM line"
        return
    table = raw.get("environment")
    prebuilt = isinstance(table, dict) and table.get("docker_image")
    if not prebuilt and not any((build_dir / name).is_file() for name in COMPOSE_NAMES):
        yield ERROR, ("nothing says how to build the environment: no environment/Dockerfile, "
                      "no compose file, no [environment].docker_image")


def check_scripts(paths: TaskPaths) -> Iterator[Finding]:
    if paths.solution_dir.is_dir() and paths.discovered_solve_path is None:
        yield ERROR, "solution/ holds no solve.sh or solve.bat"
    if not paths.tests_dir.is_dir():
        return
    runner = paths.discovered_test_path
    if runner is None:
        yield ERROR, "tests/ holds no test.sh or test.bat"
    elif not any(name in _text(runner) for name in REWARD_NAMES):
        yield ERROR, (f"tests/{runner.name} names no reward file; harbor takes the verdict "
                      "from /logs/verifier/reward.txt or reward.json")
    if not any(paths.tests_dir.glob("*.py")):
        yield WARNING, "tests/ holds no Python test file"


def check_canary(paths: TaskPaths) -> Iterator[Finding]:
    """Benchmark data carries a canary string outside instruction.md, so it can be filtered from training sets."""
    carriers = [paths.discovered_solve_path, paths.environment_dir / "Dockerfile", *paths.tests_dir.glob("*.py")]
    if not any(p and p.is_file() and "canary" in _text(p).lower() for p in carriers):
        yield WARNING, "no canary string in the solve script, the Dockerfile or the Python tests"


def validate_task(task_dir: str) -> dict:
    """Check one task directory: {"passed": bool, "errors": [str], "warnings": [str]}."""
    if not Path(task_dir).is_dir():
        return {"passed": False, "errors": [f"{task_dir} is not a directory"], "warnings": []}
    paths = TaskPaths(task_dir)
    findings = list(check_layout(paths))
    raw: dict = {}
    if paths.config_path.is_file():
        raw, config, schema_errors = read_config(paths)
        findings += [(ERROR, message) for message in schema_errors]
        if not schema_errors:
            findings += check_policy(raw, config)
    findings += [*check_image(paths, raw), *check_scripts(paths), *check_canary(paths)]

    errors = [message for level, message in findings if level == ERROR]
    if not errors and not Task.is_valid_dir(paths.task_dir):
        errors.append("harbor does not accept the directory as a task (Task.is_valid_dir is false)")
    return {"passed": not errors, "errors": errors,
            "warnings": [message for level, message in findings if level == WARNING]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m validator.validate", description=__doc__.split("\n\n")[0].strip())
    parser.add_argument("task_dir", help="the task directory (the one that holds task.toml)")
    args = parser.parse_args(argv)

    result = validate_task(args.task_dir)
    for level in ("warnings", "errors"):
        for message in result[level]:
            print(f"{level[:-1]}: {message}")
    counts = f"{len(result['errors'])} error(s), {len(result['warnings'])} warning(s)"
    print(f"{args.task_dir}: {'passed' if result['passed'] else 'failed'}, {counts}")
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
