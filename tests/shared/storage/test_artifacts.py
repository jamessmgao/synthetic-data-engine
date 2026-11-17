"""Content-addressed bundles, LLM transcripts, traces and publishing: local and S3."""

import os
import shutil
import threading
import uuid

import pytest
from tests.shared.conftest import S3

from shared.storage.artifacts import LocalArtifactStore, S3ArtifactStore, bundle_sha256

STORES = [pytest.param("local", id="local")]
if S3:
    STORES.append(pytest.param("s3", id="s3"))


@pytest.fixture(params=STORES)
def make_store(request, tmp_path):
    bucket = f"sde-test-{uuid.uuid4().hex[:8]}"

    def make(name: str = "work"):
        root = tmp_path / name
        if request.param == "local":
            return LocalArtifactStore(root)
        return S3ArtifactStore(root, bucket, S3)

    return make


def write_task(d, text="hello"):
    (d / "environment").mkdir(parents=True)
    (d / "instruction.md").write_text(text)
    (d / "environment" / "Dockerfile").write_text("FROM debian:bookworm-slim\n")
    sh = d / "solve.sh"
    sh.write_text("#!/bin/bash\necho ok\n")
    sh.chmod(0o755)
    return d


def test_bundle_hash_covers_content_and_mode(tmp_path):
    a = write_task(tmp_path / "a")
    b = write_task(tmp_path / "b")
    assert bundle_sha256(a) == bundle_sha256(b)
    before = bundle_sha256(a)
    (b / "solve.sh").chmod(0o644)
    assert bundle_sha256(b) != before
    (a / "instruction.md").write_text("changed")
    assert bundle_sha256(a) != before


def test_put_bundle_is_idempotent_and_concurrent_safe(make_store, tmp_path):
    store = make_store()
    src = write_task(tmp_path / "src")
    shas = []
    threads = [threading.Thread(target=lambda: shas.append(store.put_bundle(src))) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(set(shas)) == 1
    path = store.bundle_path(shas[0])
    assert (path / "instruction.md").read_text() == "hello"
    assert os.access(path / "solve.sh", os.X_OK)
    assert not [p for p in path.parent.iterdir() if p.name.startswith(".")]


def test_second_machine_downloads_bundle_and_transcript(make_store, tmp_path):
    first = make_store("machine-a")
    sha = first.put_bundle(write_task(tmp_path / "src"))
    first.put_llm("req1", {"response": "draft", "usage": {"cost": 0.01}})
    if isinstance(first, S3ArtifactStore):
        second = make_store("machine-b")  # empty local cache, same bucket
        path = second.bundle_path(sha)
        assert (path / "instruction.md").read_text() == "hello"
        assert os.access(path / "solve.sh", os.X_OK)
        assert second.get_llm("req1")["usage"]["cost"] == 0.01
    else:
        assert first.get_llm("req1")["response"] == "draft"
    assert first.get_llm("missing") is None


def test_abandoned_trace_is_kept_not_deleted(make_store):
    store = make_store()
    (store.trace_dir("job-1")).mkdir(parents=True)
    (store.trace_dir("job-1") / "partial.log").write_text("x")
    first = store.abandon_trace("job-1")
    store.trace_dir("job-1").mkdir()
    second = store.abandon_trace("job-1")
    assert first.name == "job-1.abandoned-1" and second.name == "job-1.abandoned-2"
    assert (first / "partial.log").read_text() == "x"


def test_publish_is_atomic_and_idempotent(make_store, tmp_path):
    store = make_store()
    sha = store.put_bundle(write_task(tmp_path / "src"))
    dest = tmp_path / "output" / "accepted" / "my-task"
    store.publish(sha, dest, "# ACCEPTED\n")
    store.publish(sha, dest, "# second call is a no-op\n")
    assert (dest / "verdict.md").read_text() == "# ACCEPTED\n"
    assert (dest / "instruction.md").exists()
    shutil.rmtree(dest)
