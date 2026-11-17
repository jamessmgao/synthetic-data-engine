"""Backend matrix for the infrastructure tests.

Always runs on SQLite + files. The production adapters join the matrix when their
test services are reachable (`make test-infra` brings them up with compose):

    SDE_TEST_DATABASE_URL  postgresql://sde:sde@localhost:5432/sde_test
    SDE_TEST_REDIS_URL     redis://localhost:6379/15
    SDE_TEST_S3_ENDPOINT   http://localhost:9000   (S3-compatible, e.g. compose's RustFS)
"""

import os
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from shared.storage import from_env  # noqa: E402
from shared.storage.sql import utcnow  # noqa: E402
from shared.jobs.jobqueue import JobQueue  # noqa: E402

PG = os.environ.get("SDE_TEST_DATABASE_URL")
REDIS = os.environ.get("SDE_TEST_REDIS_URL")
S3 = os.environ.get("SDE_TEST_S3_ENDPOINT")

MATRIX = [pytest.param({}, id="sqlite")]
if PG:
    MATRIX.append(pytest.param({"db": PG}, id="postgres"))
if PG and REDIS:
    MATRIX.append(pytest.param({"db": PG, "redis": REDIS}, id="postgres+redis"))


def _reset_postgres(url: str) -> None:
    import psycopg

    with psycopg.connect(url, autocommit=True) as c:
        c.execute("DROP TABLE IF EXISTS jobs, tasks")


def make_env(tmp_path: Path, cfg: dict[str, str]) -> dict[str, str]:
    env = {"SDE_WORK_DIR": str(tmp_path / "work")}
    if db := cfg.get("db"):
        _reset_postgres(db)
        env["DATABASE_URL"] = db
    if redis_url := cfg.get("redis"):
        import redis

        redis.Redis.from_url(redis_url).flushdb()
        env["REDIS_URL"] = redis_url
    return env


@pytest.fixture(params=MATRIX)
def env(request, tmp_path):
    return make_env(tmp_path, request.param)


@pytest.fixture
def backend(env):
    return from_env(env)


@pytest.fixture
def jq(backend):
    return JobQueue(backend)


def add_task(backend, task_id: str | None = None, batch_id: str = "b-test", status: str = "CALIBRATING") -> str:
    """Insert a task row directly (task creation belongs to the Job Service)."""
    task_id = task_id or f"{batch_id}-{uuid.uuid4().hex[:6]}"
    now = utcnow()
    with backend.store.tx() as c:
        c.execute(
            """INSERT INTO tasks (id, batch_id, status, stage, created_at, updated_at)
               VALUES (?, ?, ?, 'QUEUED', ?, ?)""",
            (task_id, batch_id, status, now, now),
        )
    return task_id
