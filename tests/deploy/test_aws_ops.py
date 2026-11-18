"""aws_ops.runners() with a Terraform state mid-apply or partly failed: null instance ids are
skipped (with a warning), so join / deploy / exec don't fail at SSM on them."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "deploy"))
import aws_ops  # noqa: E402


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
