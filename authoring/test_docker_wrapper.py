"""authoring/bin/docker: the agent's Docker CLI, restricted to its own trial's objects.
Run against a fake docker.real that records its argv and answers `inspect` from a map."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

WRAPPER = Path(__file__).resolve().parent / "bin" / "docker"
LABEL = "sde.job=t1-v1-world"

FAKE_REAL = r'''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as f:
    f.write(json.dumps(args) + "\n")
objects = json.loads(os.environ["FAKE_OBJECTS"])  # name -> {"kind": ..., "labels": {...}}
if args[:1] == ["inspect"]:
    kind = args[args.index("--type") + 1] if "--type" in args else None
    obj = objects.get(args[-1])
    if obj is None or (kind and obj["kind"] != kind):
        sys.exit(1)
    print(json.dumps(obj["labels"]))
    sys.exit(0)
if "ps" in args and "-sde" in args:  # compose ps -sde
    print("\n".join(n for n, o in objects.items() if o.get("compose")))
sys.exit(0)
'''

OBJECTS = {
    "mine": {"kind": "container", "labels": {"sde.job": "t1-v1-world"}},
    "pipeline-api": {"kind": "container", "labels": {"com.docker.compose.project": "sde-local"}},
    "other-trial": {"kind": "container", "labels": {"sde.job": "t2-v1-world"}},
    "my-image": {"kind": "image", "labels": {"sde.job": "t1-v1-world"}},
    "python:3.12": {"kind": "image", "labels": {}},
}


@pytest.fixture
def docker(tmp_path):
    real = tmp_path / "docker.real"
    real.write_text(FAKE_REAL)
    real.chmod(0o755)
    log = tmp_path / "log.jsonl"

    def run(*args, label=LABEL, objects=OBJECTS, **env):
        log.write_text("")
        e = {**os.environ, "SDE_DOCKER_REAL": str(real), "SDE_DOCKER_LABEL": label, "FAKE_LOG": str(log),
             "FAKE_OBJECTS": json.dumps(objects), **env}
        e.pop("DOCKER_HOST", None) if "DOCKER_HOST" not in env else None
        p = subprocess.run([sys.executable, str(WRAPPER), *args], capture_output=True, text=True, env=e)
        calls = [json.loads(line) for line in log.read_text().splitlines()]
        # the wrapper's own lookups (inspect -f ..., compose ps -sde) aren't the command it ran
        final = [c for c in calls if not (c[:1] == ["inspect"] and "-f" in c) and "-sde" not in c]
        return p.returncode, p.stderr, final[-1] if final else None
    return run


def test_killing_all_containers_kills_only_this_trials(docker):
    """The incident: `docker kill $(docker ps -q)`. ps only lists this trial's containers..."""
    code, _, sent = docker("ps", "-q")
    assert code == 0 and sent == ["ps", "--filter", f"label={LABEL}", "-q"]
    # ...and a kill of anything else is refused, the trial's own goes through.
    code, err, sent = docker("kill", "pipeline-api")
    assert code == 1 and "refused" in err and "pipeline-api" in err and sent is None
    assert docker("kill", "mine")[0] == 0 and docker("kill", "mine")[2] == ["kill", "mine"]
    code, _, sent = docker("kill", "mine", "other-trial")  # one foreign target refuses the whole command
    assert code == 1 and sent is None


@pytest.mark.parametrize("args", [
    ("stop", "-t", "5", "pipeline-api"), ("rm", "-f", "pipeline-api"), ("restart", "pipeline-api"),
    ("exec", "pipeline-api", "sh", "-c", "cat /etc/secrets"), ("logs", "--tail", "50", "pipeline-api"),
    ("inspect", "-f", "{{.Config.Env}}", "pipeline-api"), ("cp", "pipeline-api:/data", "."),
    ("container", "rm", "pipeline-api"), ("update", "--cpus", "4", "pipeline-api"), ("rmi", "python:3.12"),
])
def test_commands_on_containers_or_images_it_did_not_start_are_refused(docker, args):
    code, err, sent = docker(*args)
    assert code == 1 and "refused" in err and sent is None


@pytest.mark.parametrize("args", [
    ("stop", "-t", "5", "mine"), ("rm", "-f", "mine"), ("exec", "-u", "root", "mine", "ls"),
    ("logs", "--tail", "50", "mine"), ("cp", "./x", "mine:/app/x"), ("container", "rm", "mine"),
    ("rmi", "my-image"), ("inspect", "python:3.12"),  # reading an image touches nothing
])
def test_its_own_objects_are_allowed(docker, args):
    code, err, sent = docker(*args)
    assert code == 0, err
    assert sent is not None or args[0] == "inspect"  # the helper hides inspect calls (the ownership lookups)


def test_run_create_and_build_get_the_trials_label(docker):
    assert docker("run", "--rm", "img", "true")[2] == ["run", "--label", LABEL, "--rm", "img", "true"]
    assert docker("build", "-t", "w", ".")[2] == ["build", "--label", LABEL, "-t", "w-t1-v1-world", "."]
    assert docker("network", "create", "n1")[2] == ["network", "create", "--label", LABEL, "n1"]
    already = docker("run", "--label", LABEL, "img")[2]
    assert already.count(LABEL) == 1


@pytest.mark.parametrize("args,why", [
    (("system", "prune", "-af"), "prune"), (("container", "prune"), "prune"), (("image", "prune", "-a"), "prune"),
    (("network", "prune"), "prune"), (("volume", "prune"), "prune"), (("builder", "prune"), "prune"),
    (("-H", "tcp://other:2375", "ps"), "-H"), (("--context", "prod", "ps"), "--context"),
    (("run", "-v", "/:/host", "img"), "host bind mount"), (("run", "--privileged", "img"), "--privileged"),
    (("run", "--pid=host", "img"), "--pid host"), (("run", "--mount", "type=bind,src=/,dst=/h", "img"), "bind"),
])
def test_prunes_other_daemons_and_host_access_are_refused(docker, args, why):
    code, err, sent = docker(*args)
    assert code == 1 and why in err and sent is None


def test_docker_host_in_the_environment_is_refused(docker):
    code, err, _ = docker("ps", DOCKER_HOST="tcp://other:2375")
    assert code == 1 and "DOCKER_HOST" in err


def test_compose_down_only_when_every_container_is_ours(docker):
    ours = {"mine": {**OBJECTS["mine"], "compose": True}}
    assert docker("compose", "down", objects=ours)[0] == 0
    mixed = {**ours, "pipeline-api": {**OBJECTS["pipeline-api"], "compose": True}}
    code, err, _ = docker("compose", "down", objects=mixed)
    assert code == 1 and "pipeline-api" in err


def test_read_only_commands_pass_and_without_a_label_targets_are_refused(docker):
    assert docker("version")[2] == ["version"] and docker("images")[2] == ["images"]
    code, err, _ = docker("kill", "mine", label="")
    assert code == 1 and "SDE_DOCKER_LABEL is not set" in err
    assert docker("ps", label="")[0] == 1


# Two trials sharing a daemon both build `sde-world-dev` (the world prompt's name); each must
# get its own image back, and base images must be left alone.
SCOPED = {**OBJECTS,
          "sde-world-dev-t1-v1-world": {"kind": "image", "labels": {"sde.job": "t1-v1-world"}},
          "sde-world-dev-t2-v1-world": {"kind": "image", "labels": {"sde.job": "t2-v1-world"}}}


@pytest.mark.parametrize("args,expect", [
    (("build", "-t", "sde-world-dev", "environment/"), ["-t", "sde-world-dev-t1-v1-world", "environment/"]),
    (("build", "--tag=app:1", "."), ["--tag=app-t1-v1-world:1", "."]),
    (("buildx", "build", "-t", "localhost:5000/app", "."), ["-t", "localhost:5000/app-t1-v1-world", "."]),
    (("build", "-t", "sde-world-dev-t1-v1-world", "."), ["-t", "sde-world-dev-t1-v1-world", "."]),  # no double suffix
])
def test_build_names_are_per_trial(docker, args, expect):
    code, err, sent = docker(*args)
    assert code == 0, err
    assert sent[-len(expect):] == expect


@pytest.mark.parametrize("args,expect", [
    (("run", "--rm", "-e", "X=1", "-v", "vol:/data", "sde-world-dev", "sh", "-c", "ls /app"),
     ["run", "--label", LABEL, "--rm", "-e", "X=1", "-v", "vol:/data", "sde-world-dev-t1-v1-world", "sh", "-c", "ls /app"]),
    (("create", "--name", "c1", "sde-world-dev", "sleep", "300"),
     ["create", "--label", LABEL, "--name", "c1", "sde-world-dev-t1-v1-world", "sleep", "300"]),
    (("run", "-it", "--rm", "python:3.12", "python"), ["run", "--label", LABEL, "-it", "--rm", "python:3.12", "python"]),
    (("tag", "sde-world-dev", "snap"), ["tag", "sde-world-dev-t1-v1-world", "snap-t1-v1-world"]),
    (("image", "inspect", "sde-world-dev"), ["inspect", "sde-world-dev-t1-v1-world"]),
    (("rmi", "sde-world-dev"), ["rmi", "sde-world-dev-t1-v1-world"]),
])
def test_image_references_resolve_to_the_trials_own_build(docker, args, expect):
    code, err, sent = docker(*args, objects=SCOPED)
    assert code == 0, err
    assert sent == expect


def test_another_trials_build_is_never_used(docker):
    """Trial t3 has built nothing yet: `sde-world-dev` must not resolve to t1's or t2's image."""
    code, _, sent = docker("run", "--rm", "sde-world-dev", "true", label="sde.job=t3-v1-world", objects=SCOPED)
    assert code == 0 and sent[-2:] == ["sde-world-dev", "true"]
    code, _, sent = docker("run", "--rm", "sde-world-dev", "true", label="sde.job=t2-v1-world", objects=SCOPED)
    assert sent[-2:] == ["sde-world-dev-t2-v1-world", "true"]
