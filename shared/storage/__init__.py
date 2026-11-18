"""Backend adapters, chosen by environment (PRD §5.4). Same code locally and in prod:

| Env var                          | Unset (local default)        | Set (compose / prod)         |
|----------------------------------|------------------------------|------------------------------|
| DATABASE_URL                     | sqlite:///work/sde.db         | postgresql://…               |
| REDIS_URL (+ SDE_REDIS_PREFIX)    | queue = the jobs table       | single-node Redis sorted sets|
| S3_BUCKET (+ S3_ENDPOINT_URL)    | files under work/            | S3, or a local S3 via the endpoint|
| SDE_WORK_DIR                      | <repo>/work                  | local cache dir              |
"""

import os
from dataclasses import dataclass
from pathlib import Path

from shared.storage.artifacts import LocalArtifactStore, S3ArtifactStore
from shared.storage.queue import Queue, make_queue
from shared.storage.sql import SqlStore

REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass
class Backend:
    store: SqlStore
    queue: Queue
    artifacts: LocalArtifactStore
    work_dir: Path


def from_env(env: dict[str, str] | None = None) -> Backend:
    env = dict(os.environ if env is None else env)
    work_dir = Path(env.get("SDE_WORK_DIR") or REPO_ROOT / "work").resolve()
    store = SqlStore(env.get("DATABASE_URL") or f"sqlite:///{work_dir / 'sde.db'}")
    queue = make_queue(store, env.get("REDIS_URL") or None, env.get("SDE_REDIS_PREFIX") or "sde:")
    if bucket := env.get("S3_BUCKET"):
        artifacts: LocalArtifactStore = S3ArtifactStore(
            work_dir, bucket, env.get("S3_ENDPOINT_URL") or None
        )
    else:
        artifacts = LocalArtifactStore(work_dir)
    return Backend(store=store, queue=queue, artifacts=artifacts, work_dir=work_dir)


def describe(backend: Backend) -> dict[str, str]:
    """Which adapters are active, without secrets (for logs and /healthz)."""
    url = backend.store.url
    return {
        "store": url.split("://")[0] if "://" in url else url,
        "queue": type(backend.queue).__name__,
        "artifacts": type(backend.artifacts).__name__,
    }
