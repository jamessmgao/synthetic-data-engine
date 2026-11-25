"""Every setting the services read reaches their containers (a local stack once ran
with no budget cap because compose.dev passed only three of them)."""

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
# Set per process or per container, not by the operator.
NOT_SETTINGS = {"SDE_API_KEY", "SDE_DOCKER_LABEL", "SDE_ENV_FILE", "SDE_RELEASE", "SDE_WORK_DIR",
                "SDE_MOCK_AUTHOR_STEPS", "SDE_MOCK_AUTHOR_STEP_S", "SDE_MOCK_FIXTURE",
                # sde-task's own knob (shared/taskformat/task_check.py), read where sde-task runs:
                # in the authoring container. It was never a compose setting.
                "SDE_TASK_PARALLEL"}


def settings_in_code() -> set[str]:
    names = set()
    for d in ("shared", "generator", "evaluation", "validator", "api"):
        for f in (ROOT / d).rglob("*.py"):
            if not f.name.startswith("test_"):
                names |= set(re.findall(r'"(SDE_[A-Z0-9_]+)"', f.read_text()))
    return {n for n in names if not n.endswith("_CMD")} - NOT_SETTINGS


def test_compose_dev_reads_local_env_in_every_app_service():
    services = yaml.safe_load((ROOT / "deploy" / "compose.dev.yaml").read_text())["services"]
    for name in ("api", "worker-control", "worker-runner"):
        assert services[name].get("env_file") == [{"path": "make.env", "required": False},
                                                  {"path": "local.env", "required": False}], name


def test_no_environment_entry_shadows_a_setting():
    """compose's `environment:` beats env_file, so a default there silently wins over local.env."""
    services = yaml.safe_load((ROOT / "deploy" / "compose.dev.yaml").read_text())["services"]
    for name in ("api", "worker-control", "worker-runner"):
        assert settings_in_code() & set(services[name].get("environment") or {}) == set(), name


def test_local_env_example_lists_every_setting():
    listed = set(re.findall(r"^# (SDE_[A-Z0-9_]+)=", (ROOT / "deploy" / "local.env.example").read_text(), re.M))
    assert settings_in_code() - listed == set()


def test_compose_prod_passes_the_whole_runtime_env():
    services = yaml.safe_load((ROOT / "deploy" / "compose.prod.yaml").read_text())["services"]
    for name in ("api", "worker-control", "worker-runner"):
        assert services[name].get("env_file") == "/opt/sde/runtime.env", name
