"""Content-addressed, write-once artifacts: the project `ArtifactStore` (PRD §3, §6.7).

Layout under the work dir:

    bundles/<sha256>/          immutable task directories
    llm/<request_sha256>.json  prompt + response + usage of one LLM call
    traces/<job_id>/           Harbor job dir(s) of one job

Every write goes to a temp path and is renamed into place, so a reader never sees
a half-written artifact and two processes writing the same key are harmless.
"""

import hashlib
import json
import os
import shutil
import uuid
from pathlib import Path
from typing import Any


def bundle_sha256(task_dir: Path) -> str:
    """Hash of every file's relative path, mode bit and bytes, in sorted order."""
    h = hashlib.sha256()
    for path in sorted(p for p in task_dir.rglob("*") if p.is_file()):
        rel = path.relative_to(task_dir).as_posix()
        executable = os.access(path, os.X_OK)
        h.update(f"{rel}\0{int(executable)}\0".encode())
        h.update(hashlib.sha256(path.read_bytes()).digest())
    return h.hexdigest()


def _atomic_copytree(src: Path, dest: Path) -> None:
    if dest.exists():
        return
    tmp = dest.with_name(f".{dest.name}.tmp-{uuid.uuid4().hex}")
    shutil.copytree(src, tmp)
    try:
        tmp.rename(dest)
    except OSError:
        # Another process renamed the same content into place first.
        shutil.rmtree(tmp, ignore_errors=True)
        if not dest.exists():
            raise


class LocalArtifactStore:
    """Artifacts on local disk (the project default)."""

    def __init__(self, root: Path):
        self.root = root
        for sub in ("bundles", "llm", "traces"):
            (root / sub).mkdir(parents=True, exist_ok=True)

    # --- bundles ---------------------------------------------------------

    def put_bundle(self, task_dir: Path) -> str:
        sha = bundle_sha256(task_dir)
        _atomic_copytree(task_dir, self.bundle_path(sha))
        return sha

    def bundle_path(self, sha: str) -> Path:
        return self.root / "bundles" / sha

    def register_bundle(self, sha: str) -> None:
        """Make a bundle another component already wrote to `bundle_path(sha)` durable
        (the generator content-addresses bundles itself). A no-op locally."""
        if not self.bundle_path(sha).is_dir():
            raise FileNotFoundError(self.bundle_path(sha))

    # --- LLM transcripts ---------------------------------------------------

    @staticmethod
    def request_sha256(request: dict[str, Any]) -> str:
        return hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()

    def get_llm(self, request_sha: str) -> dict[str, Any] | None:
        path = self.root / "llm" / f"{request_sha}.json"
        return json.loads(path.read_text()) if path.exists() else None

    def put_llm(self, request_sha: str, record: dict[str, Any]) -> None:
        path = self.root / "llm" / f"{request_sha}.json"
        tmp = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex}")
        tmp.write_text(json.dumps(record, indent=2, sort_keys=True))
        os.replace(tmp, path)

    # --- traces ------------------------------------------------------------

    def trace_dir(self, job_id: str) -> Path:
        return self.root / "traces" / job_id

    def save_trace(self, job_id: str) -> str:
        """Persist a finished job's trace dir; returns its trace URI."""
        return f"traces/{job_id}/"

    def abandon_trace(self, job_id: str) -> Path | None:
        """Move a half-finished trace dir aside (kept for the audit, never deleted)."""
        src = self.trace_dir(job_id)
        if not src.exists():
            return None
        n = 1
        while (dest := src.with_name(f"{job_id}.abandoned-{n}")).exists():
            n += 1
        src.rename(dest)
        return dest

    # --- publishing ----------------------------------------------------------

    def publish(self, sha: str, dest: Path, verdict_md: str) -> None:
        """Copy an accepted bundle plus its verdict to `dest`, atomically."""
        if dest.exists():
            return
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(f".{dest.name}.tmp-{uuid.uuid4().hex}")
        shutil.copytree(self.bundle_path(sha), tmp)
        (tmp / "verdict.md").write_text(verdict_md)
        try:
            tmp.rename(dest)
        except OSError:
            shutil.rmtree(tmp, ignore_errors=True)
            if not dest.exists():
                raise


class S3ArtifactStore(LocalArtifactStore):
    """S3 (or an S3-compatible server) as the durable copy, with the local work dir as a cache.

    Harbor and Docker need real files, so everything is also kept locally; S3 is
    what survives losing a machine and what other workers download from. Keys
    mirror the local layout (`bundles/<sha>/…`, `llm/<sha>.json`, `traces/<job>/…`,
    `accepted/<name>/…`). A bundle counts as uploaded only once its
    `bundles/<sha>/.complete` marker exists, so a half-finished upload is redone.
    """

    def __init__(self, root: Path, bucket: str, endpoint_url: str | None = None):
        import boto3
        from botocore.exceptions import ClientError

        super().__init__(root)
        self.bucket = bucket
        self.s3 = boto3.client("s3", endpoint_url=endpoint_url)
        self._ClientError = ClientError
        try:
            self.s3.head_bucket(Bucket=bucket)
        except ClientError:
            try:
                self.s3.create_bucket(Bucket=bucket)
            except ClientError as e:  # another process created it first
                if e.response["Error"]["Code"] not in ("BucketAlreadyOwnedByYou", "BucketAlreadyExists"):
                    raise

    def _exists(self, key: str) -> bool:
        try:
            self.s3.head_object(Bucket=self.bucket, Key=key)
            return True
        except self._ClientError:
            return False

    def _upload_dir(self, src: Path, prefix: str) -> None:
        for path in sorted(p for p in src.rglob("*") if p.is_file()):
            key = f"{prefix}/{path.relative_to(src).as_posix()}"
            extra = {"Metadata": {"executable": "1"}} if os.access(path, os.X_OK) else {}
            self.s3.upload_file(str(path), self.bucket, key, ExtraArgs=extra)

    def _download_dir(self, prefix: str, dest: Path) -> None:
        tmp = dest.with_name(f".{dest.name}.tmp-{uuid.uuid4().hex}")
        paginator = self.s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=f"{prefix}/"):
            for obj in page.get("Contents", []):
                rel = obj["Key"].removeprefix(f"{prefix}/")
                if rel == ".complete":
                    continue
                target = tmp / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                self.s3.download_file(self.bucket, obj["Key"], str(target))
                head = self.s3.head_object(Bucket=self.bucket, Key=obj["Key"])
                if head.get("Metadata", {}).get("executable") == "1":
                    target.chmod(0o755)
        try:
            tmp.rename(dest)
        except OSError:
            shutil.rmtree(tmp, ignore_errors=True)

    def put_bundle(self, task_dir: Path) -> str:
        sha = super().put_bundle(task_dir)
        self.register_bundle(sha)
        return sha

    def register_bundle(self, sha: str) -> None:
        super().register_bundle(sha)
        if not self._exists(f"bundles/{sha}/.complete"):
            self._upload_dir(self.bundle_path(sha), f"bundles/{sha}")
            self.s3.put_object(Bucket=self.bucket, Key=f"bundles/{sha}/.complete", Body=b"")

    def bundle_path(self, sha: str) -> Path:
        local = super().bundle_path(sha)
        if not local.exists() and self._exists(f"bundles/{sha}/.complete"):
            self._download_dir(f"bundles/{sha}", local)
        return local

    def get_llm(self, request_sha: str) -> dict[str, Any] | None:
        found = super().get_llm(request_sha)
        if found is None and self._exists(f"llm/{request_sha}.json"):
            body = self.s3.get_object(Bucket=self.bucket, Key=f"llm/{request_sha}.json")["Body"]
            found = json.loads(body.read())
            super().put_llm(request_sha, found)
        return found

    def put_llm(self, request_sha: str, record: dict[str, Any]) -> None:
        super().put_llm(request_sha, record)
        self.s3.put_object(Bucket=self.bucket, Key=f"llm/{request_sha}.json",
                           Body=json.dumps(record, indent=2, sort_keys=True).encode())

    def save_trace(self, job_id: str) -> str:
        src = self.trace_dir(job_id)
        if src.exists():
            self._upload_dir(src, f"traces/{job_id}")
        return f"s3://{self.bucket}/traces/{job_id}/"

    def publish(self, sha: str, dest: Path, verdict_md: str) -> None:
        super().publish(sha, dest, verdict_md)
        if not self._exists(f"accepted/{dest.name}/verdict.md"):
            self._upload_dir(dest, f"accepted/{dest.name}")
