"""Content-addressed bundles, LLM transcripts, traces and publishing: local and S3."""

import os
import shutil
import threading
import time
import uuid

import pytest
from tests.shared.conftest import S3

from shared.storage.artifacts import (
    LocalArtifactStore,
    S3ArtifactStore,
    bundle_sha256,
    staging_path,
    sweep_staging,
)

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
    assert [p.name for p in path.parent.iterdir()] in ([shas[0], ".staging"], [".staging", shas[0]])
    assert not list((path.parent / ".staging").iterdir())


def test_killed_writers_leave_only_staging_entries_which_get_swept(make_store, tmp_path):
    store = make_store()
    accepted = tmp_path / "output" / "accepted"
    sha = store.put_bundle(write_task(tmp_path / "src"))
    # A publish killed between copy and rename leaves its staged copy behind…
    stale = staging_path(accepted / "my-task")
    shutil.copytree(store.bundle_path(sha), stale)
    fresh = staging_path(store.root / "llm" / "abc.json")
    fresh.write_text("{}")
    # …but never a stray entry in the published dir itself.
    assert [p.name for p in accepted.iterdir()] == [".staging"]
    # copytree copied the source's old mtime: the age must come from the name.
    os.utime(stale, (0, 0))
    os.utime(fresh, (0, 0))
    assert sweep_staging([*store.staging_dirs(), accepted], older_than_s=60) == 0
    later = time.time() + 120
    real_time = time.time
    time.time = lambda: later
    try:
        assert sweep_staging([*store.staging_dirs(), accepted], older_than_s=60) == 2
    finally:
        time.time = real_time
    assert not stale.exists() and not fresh.exists()
    store.publish(sha, accepted / "my-task", "# ok\n")
    assert sorted(p.name for p in accepted.iterdir()) == [".staging", "my-task"]


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


def test_trace_saved_on_one_machine_is_adopted_on_another(make_store):
    m1 = make_store("m1")
    (m1.trace_dir("job-r")).mkdir(parents=True)
    (m1.trace_dir("job-r") / "result.json").write_text('{"reward": 1}')
    m1.save_trace("job-r")
    assert m1.fetch_trace("job-r")
    if isinstance(m1, S3ArtifactStore):
        m2 = make_store("m2")
        assert m2.fetch_trace("job-r")
        assert (m2.trace_dir("job-r") / "result.json").read_text() == '{"reward": 1}'
        assert not (m2.trace_dir("job-r") / ".complete").exists()
    assert not m1.fetch_trace("never-ran")


def test_half_uploaded_trace_is_not_adopted_and_is_moved_aside(make_store):
    m1 = make_store("m1")
    if not isinstance(m1, S3ArtifactStore):
        return
    # m1 died mid-upload: files are in S3 but there is no .complete marker.
    m1.s3.put_object(Bucket=m1.bucket, Key="traces/job-h/partial.log", Body=b"x")
    m2 = make_store("m2")
    assert not m2.fetch_trace("job-h")
    m2.abandon_trace("job-h")
    keys = [o["Key"] for o in m2.s3.list_objects_v2(Bucket=m2.bucket, Prefix="traces/").get("Contents", [])]
    assert keys == ["traces/job-h.abandoned-1/partial.log"]
    m2.trace_dir("job-h").mkdir(parents=True)
    (m2.trace_dir("job-h") / "result.json").write_text("{}")
    m2.save_trace("job-h")
    assert make_store("m3").fetch_trace("job-h")


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
