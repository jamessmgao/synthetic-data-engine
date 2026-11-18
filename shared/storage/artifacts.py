"""Content-addressed, write-once artifacts: the project `ArtifactStore` (PRD §3, §6.7).

Layout under the work dir:

    bundles/<sha256>/          immutable task directories
    worlds/<sha256>/           immutable world directories (agent mode, plan §3.3)
    drafts/<sha256>.json       a bundle's draft record (prompt + TaskDraft)
    llm/<request_sha256>.json  prompt + response + usage of one LLM call
    traces/<job_id>/           Harbor job dir(s) of one job

Every write is staged in a `.staging/` dir next to its destination and renamed
into place, so a reader never sees a half-written artifact and two processes
writing the same key are harmless. A process killed mid-write leaves its staging
entry behind; the reconciler removes entries older than the lease TTL
(`sweep_staging`).
"""

import hashlib
import json
import os
import shutil
import time
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


STAGING = ".staging"
TREE_KINDS = ("bundles", "worlds")


def staging_path(dest: Path) -> Path:
    """A fresh temp path on the same filesystem as `dest` (so rename is atomic),
    kept out of the destination dir's listing."""
    stage = dest.parent / STAGING
    stage.mkdir(parents=True, exist_ok=True)
    # The creation time is in the name: copytree copies the source's mtime onto
    # the staged dir, so mtime can't tell a live copy from an abandoned one.
    return stage / f"{dest.name}.{int(time.time())}.{uuid.uuid4().hex}"


def _staged_at(entry: Path) -> float | None:
    parts = entry.name.rsplit(".", 2)
    try:
        return float(parts[-2]) if len(parts) == 3 else None
    except ValueError:
        return None


def sweep_staging(dirs: list[Path], older_than_s: float) -> int:
    """Delete staging entries older than `older_than_s` (their writer died).
    Returns how many were removed."""
    cutoff = time.time() - older_than_s
    removed = 0
    for d in dirs:
        stage = d / STAGING
        if not stage.is_dir():
            continue
        for entry in stage.iterdir():
            try:
                staged = _staged_at(entry)
                if (staged if staged is not None else entry.lstat().st_mtime) >= cutoff:
                    continue
                if entry.is_dir() and not entry.is_symlink():
                    shutil.rmtree(entry)
                else:
                    entry.unlink()
                removed += 1
            except FileNotFoundError:
                pass  # another sweeper got it
    return removed


def _atomic_copytree(src: Path, dest: Path) -> None:
    if dest.exists():
        return
    tmp = staging_path(dest)
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
        for sub in TREE_KINDS + ("llm", "traces", "drafts"):
            (root / sub).mkdir(parents=True, exist_ok=True)

    # --- content-addressed trees: task bundles and worlds -------------------
    # The authoring code writes them itself (write_bundle, run_world_job) under
    # <kind>/<sha>/; `register_*` makes one durable, `*_path` makes one local.

    def tree_path(self, kind: str, sha: str) -> Path:
        return self.root / kind / sha

    def register_tree(self, kind: str, sha: str) -> None:
        """Make a tree another component already wrote durable. A no-op locally."""
        if not self.tree_path(kind, sha).is_dir():
            raise FileNotFoundError(self.tree_path(kind, sha))

    def put_bundle(self, task_dir: Path) -> str:
        sha = bundle_sha256(task_dir)
        _atomic_copytree(task_dir, self.root / "bundles" / sha)
        self.register_bundle(sha)
        return sha

    def bundle_path(self, sha: str) -> Path:
        return self.tree_path("bundles", sha)

    def register_bundle(self, sha: str) -> None:
        self.register_tree("bundles", sha)

    def world_path(self, sha: str) -> Path:
        return self.tree_path("worlds", sha)

    def register_world(self, sha: str) -> None:
        self.register_tree("worlds", sha)

    # --- per-bundle records (drafts/<sha>.json) ------------------------------

    def register_file(self, rel: str) -> None:
        """Make a file another component wrote under the work dir durable, at the
        same relative path. A no-op locally."""
        if not (self.root / rel).is_file():
            raise FileNotFoundError(self.root / rel)

    def fetch_file(self, rel: str) -> bool:
        """Make a registered file local if it exists anywhere. True if it's now local."""
        return (self.root / rel).is_file()

    # --- LLM transcripts ---------------------------------------------------

    @staticmethod
    def request_sha256(request: dict[str, Any]) -> str:
        return hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()

    def get_llm(self, request_sha: str) -> dict[str, Any] | None:
        path = self.root / "llm" / f"{request_sha}.json"
        return json.loads(path.read_text()) if path.exists() else None

    def put_llm(self, request_sha: str, record: dict[str, Any]) -> None:
        path = self.root / "llm" / f"{request_sha}.json"
        tmp = staging_path(path)
        tmp.write_text(json.dumps(record, indent=2, sort_keys=True))
        os.replace(tmp, path)

    # --- traces ------------------------------------------------------------

    def trace_dir(self, job_id: str) -> Path:
        return self.root / "traces" / job_id

    def save_trace(self, job_id: str) -> str:
        """Persist a finished job's trace dir; returns its trace URI. Call it as soon
        as the run finishes, so a worker that reclaims the job can adopt it."""
        return f"traces/{job_id}/"

    def fetch_trace(self, job_id: str) -> bool:
        """Make a saved trace local if one exists. True if `trace_dir(job_id)` now
        holds it. Locally there is nothing to fetch: the dir is there or it isn't."""
        return self.trace_dir(job_id).exists()

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

    def staging_dirs(self) -> list[Path]:
        """Dirs whose `.staging/` this store writes into (besides publish targets)."""
        return [self.root / sub for sub in (*TREE_KINDS, "llm", "traces", "drafts")]

    # --- publishing ----------------------------------------------------------

    def publish(self, sha: str, dest: Path, verdict_md: str) -> None:
        """Copy an accepted bundle plus its verdict to `dest`, atomically."""
        if dest.exists():
            return
        tmp = staging_path(dest)
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
    mirror the local layout (`bundles/<sha>/…`, `worlds/<sha>/…`, `drafts/<sha>.json`,
    `llm/<sha>.json`, `traces/<job>/…`,
    `accepted/<name>/…`). A bundle counts as uploaded only once its
    `<kind>/<sha>/.complete` marker exists, so a half-finished upload is redone.
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
        tmp = staging_path(dest)
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

    def register_tree(self, kind: str, sha: str) -> None:
        super().register_tree(kind, sha)
        if not self._exists(f"{kind}/{sha}/.complete"):
            self._upload_dir(super().tree_path(kind, sha), f"{kind}/{sha}")
            self.s3.put_object(Bucket=self.bucket, Key=f"{kind}/{sha}/.complete", Body=b"")

    def tree_path(self, kind: str, sha: str) -> Path:
        local = super().tree_path(kind, sha)
        if not local.exists() and self._exists(f"{kind}/{sha}/.complete"):
            self._download_dir(f"{kind}/{sha}", local)
        return local

    def register_file(self, rel: str) -> None:
        super().register_file(rel)
        self.s3.upload_file(str(self.root / rel), self.bucket, rel)

    def fetch_file(self, rel: str) -> bool:
        if super().fetch_file(rel):
            return True
        if not self._exists(rel):
            return False
        dest = self.root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = staging_path(dest)
        self.s3.download_file(self.bucket, rel, str(tmp))
        os.replace(tmp, dest)
        return True

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
        """Upload the trace, then write `traces/<job>/.complete`. A trace without the
        marker (its uploader died midway) is never adopted by `fetch_trace`."""
        src = self.trace_dir(job_id)
        if src.exists():
            self._upload_dir(src, f"traces/{job_id}")
            self.s3.put_object(Bucket=self.bucket, Key=f"traces/{job_id}/.complete", Body=b"")
        return f"s3://{self.bucket}/traces/{job_id}/"

    def fetch_trace(self, job_id: str) -> bool:
        """Adopt a trace another machine saved: download it if it is complete in S3
        and missing locally. True if a trace is now in `trace_dir(job_id)`."""
        local = self.trace_dir(job_id)
        if local.exists():
            return True
        if not self._exists(f"traces/{job_id}/.complete"):
            return False
        self._download_dir(f"traces/{job_id}", local)
        return local.exists()

    def abandon_trace(self, job_id: str) -> Path | None:
        """Also move a half-uploaded remote trace (no marker) aside, so the next
        run's upload can't mix with it. Kept under `traces/<job>.abandoned-<n>/`."""
        moved = super().abandon_trace(job_id)
        prefix = f"traces/{job_id}/"
        if self._exists(f"{prefix}.complete"):
            return moved
        keys = [o["Key"] for page in self.s3.get_paginator("list_objects_v2").paginate(
                    Bucket=self.bucket, Prefix=prefix) for o in page.get("Contents", [])]
        if not keys:
            return moved
        n = 1
        while self.s3.list_objects_v2(Bucket=self.bucket, Prefix=f"traces/{job_id}.abandoned-{n}/",
                                      MaxKeys=1).get("KeyCount"):
            n += 1
        for key in keys:
            dest = f"traces/{job_id}.abandoned-{n}/{key.removeprefix(prefix)}"
            self.s3.copy_object(Bucket=self.bucket, Key=dest,
                                CopySource={"Bucket": self.bucket, "Key": key})
            self.s3.delete_object(Bucket=self.bucket, Key=key)
        return moved

    def publish(self, sha: str, dest: Path, verdict_md: str) -> None:
        super().publish(sha, dest, verdict_md)
        if not self._exists(f"accepted/{dest.name}/verdict.md"):
            self._upload_dir(dest, f"accepted/{dest.name}")
