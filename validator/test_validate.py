"""validator/validate.py: the structural check on task directories built in tmp_path, and its CLI."""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from validator import validate
from validator.validate import validate_task

REPO = Path(__file__).resolve().parents[1]
FIXTURE_TASK = REPO / "tests" / "fixtures" / "p1-local-1" / "task"

TOML = """\
schema_version = "1.4"

[task]
name = "synthetic-data-engine/fix-the-ledger"
description = "Fix the rounding in the ledger."

[metadata]
difficulty = "medium"

[verifier]
timeout_sec = 900.0

[agent]
timeout_sec = 3600.0

[environment]
build_timeout_sec = 600.0
"""

FILES = {
    "task.toml": TOML,
    "instruction.md": "The ledger rounds half cents the wrong way. Fix it.\n",
    "environment/Dockerfile": "# canary 1f0e\nFROM python:3.12-slim\nWORKDIR /app\n",
    "solution/solve.sh": "#!/bin/bash\nsed -i 's/floor/round/' /app/ledger.py\n",
    "tests/test.sh": "#!/bin/bash\npytest /tests/test_outputs.py && echo 1 > /logs/verifier/reward.txt\n",
    "tests/test_outputs.py": "def test_rounding():\n    assert True\n",
}


def make_task(root: Path, *, drop=(), **overrides) -> Path:
    """A valid task under root, minus the paths in `drop`, with files replaced by `overrides`
    (keyword = the path with '/' and '.' as '_', e.g. tests_test_sh)."""
    files = dict(FILES)
    for key, text in overrides.items():
        (path,) = [p for p in FILES if p.replace("/", "_").replace(".", "_") == key]
        files[path] = text
    for path, text in files.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)  # a dropped file leaves its directory
        if path not in drop:
            (root / path).write_text(text)
    for path in drop:
        if path.endswith("/"):
            shutil.rmtree(root / path)
    return root


def errors_of(task: Path) -> list[str]:
    result = validate_task(str(task))
    assert result["passed"] is (not result["errors"])
    return result["errors"]


def toml_with(old: str, new: str) -> str:
    assert old in TOML
    return TOML.replace(old, new)


def test_valid_task_passes_with_no_findings(tmp_path):
    assert validate_task(str(make_task(tmp_path))) == {"passed": True, "errors": [], "warnings": []}


def test_repo_fixture_task_passes():
    result = validate_task(str(FIXTURE_TASK))
    assert result["passed"], result["errors"]


def test_missing_directory_is_one_error(tmp_path):
    result = validate_task(str(tmp_path / "nowhere"))
    assert not result["passed"] and len(result["errors"]) == 1 and "not a directory" in result["errors"][0]


def test_a_file_is_not_a_task_directory(tmp_path):
    (tmp_path / "task").write_text("x")
    assert not validate_task(str(tmp_path / "task"))["passed"]


@pytest.mark.parametrize("drop, expected", [
    (["task.toml"], "task.toml is not in the task directory"),
    (["instruction.md"], "instruction.md is not in the task directory"),
    (["environment/"], "environment/ is not in the task directory"),
    (["solution/"], "solution/ is not in the task directory"),
    (["tests/"], "tests/ is not in the task directory"),
    (["solution/solve.sh"], "solution/ holds no solve.sh or solve.bat"),
    (["tests/test.sh"], "tests/ holds no test.sh or test.bat"),
    (["environment/Dockerfile"], "nothing says how to build the environment"),
])
def test_each_missing_piece_is_an_error(tmp_path, drop, expected):
    errors = errors_of(make_task(tmp_path, drop=drop))
    assert len(errors) == 1 and expected in errors[0], errors


def test_an_empty_directory_reports_every_missing_piece(tmp_path):
    assert len(errors_of(tmp_path)) == 5


@pytest.mark.parametrize("overrides, expected", [
    ({"instruction_md": " \n\t\n"}, "instruction.md is blank"),
    ({"environment_Dockerfile": "RUN echo from-scratch\n"}, "environment/Dockerfile has no FROM line"),
    ({"tests_test_sh": "#!/bin/bash\npytest /tests\n"}, "tests/test.sh names no reward file"),
])
def test_file_content_errors(tmp_path, overrides, expected):
    errors = errors_of(make_task(tmp_path, **overrides))
    assert len(errors) == 1 and expected in errors[0], errors


def test_reward_json_and_lowercase_from_are_accepted(tmp_path):
    task = make_task(tmp_path, tests_test_sh="echo '{}' > /logs/verifier/reward.json\n",
                     environment_Dockerfile="# canary\nfrom python:3.12-slim\n")
    assert errors_of(task) == []


@pytest.mark.parametrize("extra", ["docker-compose.yaml", "docker-compose.yml"])
def test_a_compose_file_stands_in_for_the_dockerfile(tmp_path, extra):
    task = make_task(tmp_path, drop=["environment/Dockerfile"])
    (task / "environment" / extra).write_text("services: {}\n")
    assert errors_of(task) == []


def test_a_docker_image_stands_in_for_the_dockerfile(tmp_path):
    task = make_task(tmp_path, drop=["environment/Dockerfile"],
                     task_toml=TOML + 'docker_image = "python:3.12-slim"\n')
    assert errors_of(task) == []


def test_windows_scripts_count_for_a_windows_task(tmp_path):
    task = make_task(tmp_path, drop=["solution/solve.sh", "tests/test.sh"], task_toml=TOML + 'os = "windows"\n')
    (task / "solution" / "solve.bat").write_text("echo fixed\n")
    (task / "tests" / "test.bat").write_text("echo 1 > C:\\logs\\verifier\\reward.txt\n")
    assert errors_of(task) == []


def test_toml_syntax_error(tmp_path):
    errors = errors_of(make_task(tmp_path, task_toml="[task\nname = \n"))
    assert len(errors) == 1 and "not valid TOML" in errors[0]


@pytest.mark.parametrize("old, new, expected", [
    ("timeout_sec = 900.0", 'timeout_sec = "soon"', "at verifier.timeout_sec:"),
    ('name = "synthetic-data-engine/fix-the-ledger"', 'name = "no-org"', "at task.name:"),
    ('name = "synthetic-data-engine/fix-the-ledger"\n', "", "at task.name:"),
    ("[agent]", "[agent]\nuser = [1]", "at agent.user"),
    ("[environment]", '[environment]\nos = "plan9"', "at environment.os:"),
])
def test_harbor_schema_errors_name_the_field(tmp_path, old, new, expected):
    errors = errors_of(make_task(tmp_path, task_toml=toml_with(old, new)))
    assert errors and all("fails harbor's schema" in e for e in errors), errors
    assert any(expected in e for e in errors), errors


@pytest.mark.parametrize("old, new, expected", [
    ('[task]\nname = "synthetic-data-engine/fix-the-ledger"\ndescription = "Fix the rounding in the ledger."\n', "",
     "no [task] table"),
    ('difficulty = "medium"', "", "[metadata].difficulty is None"),
    ('difficulty = "medium"', 'difficulty = "brutal"', "[metadata].difficulty is 'brutal'"),
    ("timeout_sec = 900.0", "timeout_sec = 0", "[verifier].timeout_sec is 0"),
    ("build_timeout_sec = 600.0", "build_timeout_sec = -5", "[environment].build_timeout_sec is -5"),
    ("timeout_sec = 3600.0", "timeout_sec = 3599", "[agent].timeout_sec is 3599"),
    ("[agent]\ntimeout_sec = 3600.0", "[agent]", "[agent].timeout_sec is not set"),
    ("[agent]\ntimeout_sec = 3600.0", "", "[agent].timeout_sec is not set"),
])
def test_pipeline_policy_errors(tmp_path, old, new, expected):
    errors = errors_of(make_task(tmp_path, task_toml=toml_with(old, new)))
    assert len(errors) == 1 and expected in errors[0], errors


@pytest.mark.parametrize("difficulty", ["easy", "medium", "hard", "unknown"])
def test_every_harbor_difficulty_is_accepted(tmp_path, difficulty):
    assert errors_of(make_task(tmp_path, task_toml=toml_with('"medium"', f'"{difficulty}"'))) == []


def test_legacy_version_key_is_a_pinned_schema_version(tmp_path):
    result = validate_task(str(make_task(tmp_path, task_toml=toml_with("schema_version", "version"))))
    assert result == {"passed": True, "errors": [], "warnings": []}


@pytest.mark.parametrize("change, expected", [
    ({"task_toml": TOML.replace('schema_version = "1.4"\n', "")}, "schema_version"),
    ({"task_toml": TOML.replace("Fix the rounding in the ledger.", "")}, "[task].description is blank"),
    ({"drop": ["tests/test_outputs.py"]}, "tests/ holds no Python test file"),
    ({"environment_Dockerfile": "FROM python:3.12-slim\n"}, "no canary string"),
])
def test_warnings_do_not_fail_the_task(tmp_path, change, expected):
    result = validate_task(str(make_task(tmp_path, **change)))
    assert result["passed"] and result["errors"] == []
    assert len(result["warnings"]) == 1 and expected in result["warnings"][0], result["warnings"]


def test_a_canary_in_any_carrier_file_counts(tmp_path):
    task = make_task(tmp_path, environment_Dockerfile="FROM python:3.12-slim\n",
                     tests_test_outputs_py="# CANARY 1f0e\ndef test_rounding():\n    assert True\n")
    assert validate_task(str(task))["warnings"] == []


def test_harbor_has_the_last_word(tmp_path, monkeypatch):
    task = make_task(tmp_path)
    monkeypatch.setattr(validate.Task, "is_valid_dir", staticmethod(lambda task_dir: False))
    errors = errors_of(task)
    assert len(errors) == 1 and "Task.is_valid_dir" in errors[0]


def test_main_prints_findings_and_returns_the_exit_code(tmp_path, capsys):
    good = make_task(tmp_path / "good")
    bad = make_task(tmp_path / "bad", drop=["tests/test_outputs.py"], instruction_md="")
    assert validate.main([str(good)]) == 0
    assert "passed, 0 error(s), 0 warning(s)" in capsys.readouterr().out
    assert validate.main([str(bad)]) == 1
    out = capsys.readouterr().out
    assert "error: instruction.md is blank" in out and "warning: tests/ holds no Python test file" in out
    assert "failed, 1 error(s), 1 warning(s)" in out


@pytest.mark.parametrize("command", [
    [sys.executable, "-m", "validator.validate"],
    [sys.executable, str(REPO / "validator" / "validate.py")],
    ["bash", str(REPO / "scripts" / "validate.sh")],
], ids=["module", "path", "wrapper"])
def test_cli_exit_codes(tmp_path, command):
    good = make_task(tmp_path / "good")
    bad = make_task(tmp_path / "bad", drop=["solution/solve.sh"])
    env = {"PYTHON": sys.executable, "PATH": "/usr/bin:/bin"}

    def run(*args, cwd):
        return subprocess.run([*command, *args], cwd=cwd, env=env, capture_output=True, text=True)

    assert run(str(good), cwd=REPO).returncode == 0
    failed = run(str(bad), cwd=REPO)
    assert failed.returncode == 1 and "solution/ holds no solve.sh" in failed.stdout
    assert run(cwd=REPO).returncode == 2  # argparse: no task directory given
    if command[0] == "bash":  # the wrapper works from any directory, with a relative task path
        assert run("good", cwd=tmp_path).returncode == 0
