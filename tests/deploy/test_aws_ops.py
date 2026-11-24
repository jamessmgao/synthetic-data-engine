"""aws_ops.runners() with a Terraform state mid-apply or partly failed: null instance ids are
skipped (with a warning), so join / deploy / exec don't fail at SSM on them."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "deploy"))
import aws_ops  # noqa: E402


def test_secrets_copies_the_key_and_the_gateway_base_url_to_ssm(monkeypatch, tmp_path, capsys):
    (tmp_path / ".env").write_text("OPENROUTER_API_KEY='k-123'\nOPENROUTER_API_BASE=https://gw.example.com/v1\n")
    calls = []
    monkeypatch.setattr(aws_ops, "REPO", tmp_path)
    monkeypatch.setattr(aws_ops, "run", lambda *a, **k: "")
    monkeypatch.setattr(aws_ops, "aws", lambda *a, **k: calls.append(a))
    monkeypatch.setattr(aws_ops.subprocess, "run", lambda *a, **k: type("P", (), {"returncode": 0})())
    aws_ops.cmd_secrets()
    puts = {a[a.index("--name") + 1]: (a[a.index("--type") + 1], a[a.index("--value") + 1])
            for a in calls if a[:2] == ("ssm", "put-parameter")}
    assert puts == {"/sde/openrouter_api_key": ("SecureString", "k-123"),
                    "/sde/openrouter_api_base": ("String", "https://gw.example.com/v1")}
    out = capsys.readouterr().out
    assert "k-123" not in out and "gw.example.com" not in out


def test_secrets_refuses_an_env_without_the_gateway_base_url(monkeypatch, tmp_path):
    (tmp_path / ".env").write_text("OPENROUTER_API_KEY=k-123\n")
    calls = []
    monkeypatch.setattr(aws_ops, "REPO", tmp_path)
    monkeypatch.setattr(aws_ops, "run", lambda *a, **k: "")
    monkeypatch.setattr(aws_ops, "aws", lambda *a, **k: calls.append(a))
    with pytest.raises(SystemExit, match="OPENROUTER_API_BASE"):
        aws_ops.cmd_secrets()
    assert calls == []


def with_ids(monkeypatch, ids):
    monkeypatch.setattr(aws_ops, "outputs", lambda: {"runner_instance_ids": ids})


def test_null_runners_are_skipped_with_a_warning(monkeypatch, capsys):
    with_ids(monkeypatch, ["i-0server", "i-0a", None, "i-0b", None])
    assert aws_ops.runners() == ["i-0server", "i-0a", "i-0b"]
    assert "runners [2, 4]" in capsys.readouterr().err


def test_a_complete_state_is_unchanged(monkeypatch, capsys):
    with_ids(monkeypatch, ["i-0server", "i-0a"])
    assert aws_ops.runners() == ["i-0server", "i-0a"]
    assert capsys.readouterr().err == ""


def test_no_server_is_an_error(monkeypatch):
    with_ids(monkeypatch, [None, "i-0a"])
    with pytest.raises(SystemExit):
        aws_ops.runners()


# --- the release upload, shared by `aws_ops deploy` and `k8s_aws ship` ---------------

SHA = "0123456789abcdef0123456789abcdef01234567"


@pytest.fixture
def uploads(monkeypatch):
    """Record the git and aws commands of an upload; nothing runs."""
    calls = []

    def run(cmd, **kw):
        calls.append(("run", [c if not c.endswith("release.tar.gz") else "<tarball>" for c in cmd], kw.get("cwd")))
        return SHA if cmd[:2] == ["git", "rev-parse"] else ""

    def aws(*args, **kw):
        calls.append(("aws", [a if not a.endswith("release.tar.gz") else "<tarball>" for a in args]))
        return ""

    monkeypatch.setattr(aws_ops, "run", run)
    monkeypatch.setattr(aws_ops, "aws", aws)
    return calls


UPLOAD = [
    ("run", ["git", "rev-parse", "some-ref"], aws_ops.REPO),
    ("run", ["git", "archive", "--format=tar.gz", "-o", "<tarball>", SHA], aws_ops.REPO),
    ("aws", ["s3", "cp", "<tarball>", f"s3://the-bucket/releases/{SHA}.tar.gz"]),
]


def test_upload_release_archives_the_sha_to_its_release_key(uploads):
    assert aws_ops.upload_release("some-ref", "the-bucket") == (SHA, f"releases/{SHA}.tar.gz")
    assert uploads == UPLOAD


def test_deploy_and_ship_upload_the_same_release(uploads, monkeypatch, capsys):
    import k8s_aws

    out = {"artifacts_bucket": "the-bucket", "runner_instance_ids": ["i-0server"], "docker_slots": 5}
    monkeypatch.setattr(aws_ops, "outputs", lambda: out)
    monkeypatch.setattr(aws_ops, "on_all", lambda fn, ids: [])
    aws_ops.cmd_deploy("some-ref")
    assert uploads == UPLOAD

    del uploads[:]
    sent = []
    monkeypatch.setattr(k8s_aws, "outputs", lambda: out)
    monkeypatch.setattr(k8s_aws, "runners", lambda: ["i-0server"])
    monkeypatch.setattr(k8s_aws, "remote", lambda instance, commands, **kw: sent.append((instance, commands)))
    assert k8s_aws.ship("some-ref", server_only=True) == f"/opt/sde/releases/{SHA}"
    assert uploads == UPLOAD
    # The instance unpacks the key that was uploaded.
    assert f"s3://the-bucket/releases/{SHA}.tar.gz" in sent[0][1][1]
    capsys.readouterr()
