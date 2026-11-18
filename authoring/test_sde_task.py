"""sde-task check / run (fake Docker), and sde-world's CLI wrapper."""

import json
import sys
from pathlib import Path

import pytest

sys.path[:0] = [str(Path(__file__).resolve().parent), str(Path(__file__).resolve().parents[1] / "generator")]

from authoring import sde_task  # noqa: E402
from test_generator import VALID, draft_with  # noqa: E402

PLAUSIBLE = "take the middle element without averaging"
WRONG = f"# plausible_wrong_fix: {PLAUSIBLE}\ncat > /app/stats.py <<'EOF'\ndef median(xs):\n    return sorted(xs)[len(xs) // 2]\nEOF\n"


def write(tmp_path, draft, world_wrong=PLAUSIBLE):
    d = tmp_path / "draft.json"
    d.write_text(json.dumps(draft))
    w = tmp_path / "world"
    w.mkdir(exist_ok=True)
    (w / "world.json").write_text(json.dumps({"plausible_wrong_fix": world_wrong}))
    return d, w


def test_check_accepts_a_valid_draft_tied_to_the_world(tmp_path):
    d, w = write(tmp_path, draft_with(wrong_fix_script=WRONG))
    out = sde_task.check_draft(d, w, tmp_path / "out")
    assert out["ok"], out["errors"]
    assert (Path(out["bundle_dir"]) / "solution" / "wrong_fix.sh").is_file()


def test_check_reports_pipeline_errors(tmp_path):
    d, w = write(tmp_path, draft_with(test_outputs="import requests\n\ndef test_a():\n    assert True\n"))
    out = sde_task.check_draft(d, w, tmp_path / "out")
    assert not out["ok"] and any("requests" in e for e in out["errors"])


@pytest.mark.parametrize("script, expect", [
    ("", "wrong_fix_script is empty"),
    ("cat > /app/x <<'EOF'\nx\nEOF\n", "# plausible_wrong_fix: take the middle"),
])
def test_check_ties_the_wrong_fix_to_the_worlds_plausible_wrong_fix(tmp_path, script, expect):
    d, w = write(tmp_path, draft_with(wrong_fix_script=script))
    out = sde_task.check_draft(d, w, tmp_path / "out")
    assert not out["ok"] and any(expect in e for e in out["errors"])


class FakeDocker:
    """Answers docker calls; the verifier 'writes' `reward` and `ctrf`."""

    def __init__(self, reward="0", tests=(("test_median_even", "failed", "assert 2 == 2.5"),), build=0):
        self.reward, self.build, self.calls, self.images = reward, build, [], set()
        self.ctrf = {"results": {"tests": [{"name": f"t.py::{n}", "status": s, "message": m} for n, s, m in tests]}}

    def __call__(self, args, timeout=0):
        self.calls.append(args)
        if args[0] == "build":
            if self.build == 0:
                self.images.add(args[args.index("-t") + 1])
            return self.build, "" if self.build == 0 else "error"
        if args[:2] == ["image", "inspect"]:
            return (0 if args[2] in self.images else 1), ""
        if args[0] == "run":
            return 0, "cid123\n"
        if args[0] == "exec" and args[-1] == "/logs/verifier/reward.txt":
            return 0, self.reward
        if args[0] == "exec" and args[-1] == "/logs/verifier/ctrf.json":
            return 0, json.dumps(self.ctrf)
        return 0, ""


@pytest.fixture
def bundle(tmp_path):
    d, w = write(tmp_path, draft_with(wrong_fix_script=WRONG))
    return Path(sde_task.check_draft(d, w, tmp_path / "out")["bundle_dir"])


def test_run_nop_fails_because_of_the_bug(bundle, monkeypatch):
    fake = FakeDocker(reward="0")
    monkeypatch.setattr(sde_task, "_docker", fake)
    out = sde_task.run_bundle(bundle, "nop")
    assert out["ok"] and out["reward"] == 0.0 and out["tests"][0]["name"] == "test_median_even"
    assert not any(c[0] == "exec" and "/solution/solve.sh" in c for c in fake.calls), "nop runs no script"
    run = next(c for c in fake.calls if c[0] == "run")
    assert run[2:6] == ["--cpus", "1", "--memory", "2g"] and "-v" not in run
    assert [c[0] for c in fake.calls][-2:] == ["rm", "rmi"]


def test_run_nop_failing_only_on_setup_errors_is_not_ok(bundle, monkeypatch):
    monkeypatch.setattr(sde_task, "_docker", FakeDocker(reward="0", tests=(
        ("test_a", "failed", "ModuleNotFoundError: No module named 'stats'"),
        ("test_b", "failed", "FileNotFoundError: /app/data.csv"))))
    out = sde_task.run_bundle(bundle, "nop")
    assert not out["ok"] and "setup reason" in out["setup_errors"][0]


def test_run_oracle_and_wrong_run_their_scripts_as_agent(bundle, monkeypatch):
    fake = FakeDocker(reward="1", tests=(("test_median_even", "passed", ""),))
    monkeypatch.setattr(sde_task, "_docker", fake)
    assert sde_task.run_bundle(bundle, "oracle")["ok"]
    assert ["exec", "-u", "agent", "cid123", "bash", "/solution/solve.sh"] in fake.calls
    fake = FakeDocker(reward="0")
    monkeypatch.setattr(sde_task, "_docker", fake)
    assert sde_task.run_bundle(bundle, "wrong")["ok"]
    assert ["exec", "-u", "agent", "cid123", "bash", "/solution/wrong_fix.sh"] in fake.calls


def test_run_reports_a_missing_verdict_and_a_build_failure(bundle, monkeypatch):
    monkeypatch.setattr(sde_task, "_docker", FakeDocker(reward=""))
    out = sde_task.run_bundle(bundle, "nop")
    assert not out["ok"] and "no reward" in out["setup_errors"][0]
    monkeypatch.setattr(sde_task, "_docker", FakeDocker(build=1))
    out = sde_task.run_bundle(bundle, "oracle")
    assert not out["ok"] and "does not build" in out["setup_errors"][0]


def test_labels_go_on_every_build_and_run(bundle, monkeypatch):
    monkeypatch.setenv("SDE_DOCKER_LABEL", "sde.job=t1-v1-create")
    fake = FakeDocker()
    monkeypatch.setattr(sde_task, "_docker", fake)
    sde_task.run_bundle(bundle, "nop")
    for c in fake.calls:
        if c[0] in ("build", "run"):
            assert c[c.index("--label") + 1] == "sde.job=t1-v1-create"


def kinds(fake, kind):
    return [c for c in fake.calls if c[0] == kind]


def test_all_roles_run_on_one_build_in_fresh_containers(bundle, monkeypatch):
    monkeypatch.delenv("SDE_DOCKER_LABEL", raising=False)
    fake = FakeDocker(reward="0")
    monkeypatch.setattr(sde_task, "_docker", fake)
    runs = sde_task.run_roles(bundle, ["oracle", "nop", "wrong"])
    assert [r["as"] for r in runs] == ["oracle", "nop", "wrong"]
    assert len(kinds(fake, "build")) == 1 and len(kinds(fake, "run")) == 3 and len(kinds(fake, "rm")) == 3
    assert [r["image_reused"] for r in runs] == [False, True, True]
    assert fake.calls[-1][0] == "rmi"  # outside a trial nothing is left behind


def test_inside_a_trial_the_image_is_kept_for_the_next_run(bundle, monkeypatch):
    monkeypatch.setenv("SDE_DOCKER_LABEL", "sde.job=t1-v1-create")
    fake = FakeDocker(reward="0")
    monkeypatch.setattr(sde_task, "_docker", fake)
    first = sde_task.run_bundle(bundle, "nop")
    second = sde_task.run_bundle(bundle, "wrong")  # a later `sde-task run`: same environment/, no build
    assert len(kinds(fake, "build")) == 1 and not kinds(fake, "rmi")
    assert first["image_reused"] is False and second["image_reused"] is True
    tag = kinds(fake, "build")[0][kinds(fake, "build")[0].index("-t") + 1]
    assert tag == f"sde-task-env:{sde_task.env_digest(bundle / 'environment')}"
    (bundle / "environment" / "extra.txt").write_text("changed")  # another environment/: a new build
    sde_task.run_bundle(bundle, "nop")
    assert len(kinds(fake, "build")) == 2


def test_cli_runs_several_roles_and_saves_each(tmp_path, monkeypatch, capsys):
    arts = tmp_path / "artifacts"
    arts.mkdir()
    monkeypatch.setenv("SDE_ARTIFACTS_DIR", str(arts))
    d, w = write(tmp_path, draft_with(wrong_fix_script=WRONG))
    monkeypatch.setattr(sde_task, "_docker", FakeDocker(reward="0"))
    assert sde_task.main(["run", str(d), "--as", "nop,wrong", "--world", str(w)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] and [r["as"] for r in out["runs"]] == ["nop", "wrong"]
    assert {p.name for p in (arts / "sde_task").iterdir()} == {"run-nop.json", "run-wrong.json"}
    assert sde_task.main(["run", str(d), "--as", "all", "--world", str(w)]) == 1  # oracle gets 0
    with pytest.raises(SystemExit):
        sde_task.main(["run", str(d), "--as", "oracle,bogus", "--world", str(w)])


def test_cli_exit_codes(tmp_path, monkeypatch, capsys):
    d, w = write(tmp_path, draft_with(wrong_fix_script=WRONG))
    assert sde_task.main(["check", str(d), "--world", str(w)]) == 0
    assert json.loads(capsys.readouterr().out)["ok"]
    monkeypatch.setattr(sde_task, "_docker", FakeDocker(reward="1"))  # nop passing = tests don't detect the bug
    assert sde_task.main(["run", str(d), "--as", "nop", "--world", str(w)]) == 1


def test_results_are_saved_as_self_check_evidence(tmp_path, monkeypatch, capsys):
    arts = tmp_path / "artifacts"
    arts.mkdir()
    monkeypatch.setenv("SDE_ARTIFACTS_DIR", str(arts))
    d, w = write(tmp_path, draft_with(wrong_fix_script=WRONG))
    sde_task.main(["check", str(d), "--world", str(w)])
    monkeypatch.setattr(sde_task, "_docker", FakeDocker(reward="0"))
    sde_task.main(["run", str(d), "--as", "nop", "--world", str(w)])
    assert json.loads((arts / "sde_task" / "check.json").read_text())["ok"]
    assert json.loads((arts / "sde_task" / "run-nop.json").read_text())["reward"] == 0.0


def test_schema_lists_every_draft_field(capsys):
    assert sde_task.main(["schema"]) == 0
    schema = json.loads(capsys.readouterr().out)
    assert {"instruction", "solution_script", "wrong_fix_script", "test_outputs"} <= set(schema["properties"])


def test_valid_fixture_is_unchanged():
    assert "wrong_fix_script" not in VALID  # the fixture predates it; draft_with adds it
