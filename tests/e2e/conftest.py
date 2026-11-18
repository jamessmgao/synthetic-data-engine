"""
End-to-end fixtures: whole-pipeline processes against a backend.

Every test runs on SQLite + local files. When the compose data services are up
(`make deps-up`: Postgres, Redis and S3 on localhost), the same tests also run
on the cluster, each in its own throwaway Postgres database, Redis DB and S3
bucket, so they never touch the `sde` database or the infrastructure tests'
`sde_test` / Redis DB 15. Override the addresses with SDE_E2E_PG_ADMIN_URL,
SDE_E2E_REDIS_URL and SDE_E2E_S3_ENDPOINT; set SDE_E2E_CLUSTER=0 to skip them.

No model endpoint is ever called: the fakes replace GLM-5.1 and Harbor, and every
process gets an unroutable OPENROUTER_API_BASE and a fake key, so anything that
slipped past them would fail instead of reaching the real gateway.
"""

import json
import os
import signal
import socket
import subprocess
import sys
import time
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import netguard  # noqa: E402

netguard.install()

from fastapi.testclient import TestClient  # noqa: E402

from api.app import create_app  # noqa: E402
from shared.storage import from_env  # noqa: E402

STACK = Path(__file__).with_name("stack.py")
API_KEY = "e2e-key"

PG_ADMIN = os.environ.get("SDE_E2E_PG_ADMIN_URL", "postgresql://sde:sde@localhost:5432/postgres")
REDIS = os.environ.get("SDE_E2E_REDIS_URL", "redis://localhost:6379")
# Redis DBs a test may reserve (DB 0 holds the reservations; 15 is the infra tests').
REDIS_DBS = range(1, 15)
S3 = os.environ.get("SDE_E2E_S3_ENDPOINT", "http://localhost:9000")
S3_CREDS = {"AWS_ACCESS_KEY_ID": "sde-dev", "AWS_SECRET_ACCESS_KEY": "sde-dev-secret", "AWS_DEFAULT_REGION": "us-east-1"}

# Fast pipeline timings, and no way to reach a real model.
BASE_ENV = {
    "OPENROUTER_API_KEY": "fake-e2e-key-not-a-secret",
    "OPENROUTER_API_BASE": "http://127.0.0.1:9/v1",
    "SDE_ENV_FILE": "/nonexistent/.env",
    "SDE_POLL_S": "0.05",
    "SDE_LEASE_TTL_S": "2",
    "SDE_HEARTBEAT_S": "0.3",
    "SDE_RECONCILE_S": "0.5",
    "SDE_RETRY_BACKOFF_SEC": "0.05",
    "SDE_BREAKER_INVALID": "1000",
    "SDE_DOCKER_SLOTS": "4",
    # The scenarios script versions explicitly (t04 is too hard three times, then
    # exhausted), so pin the cap rather than follow the production default.
    "SDE_MAX_VERSIONS": "3",
    "SDE_AGENT_LEASE_TTL_S": "2",  # agent-mode generation leases, as short as the others here
}


def _reachable(url: str, default_port: int) -> bool:
    u = urlparse(url)
    try:
        with socket.create_connection((u.hostname or "localhost", u.port or default_port), timeout=0.5):
            return True
    except OSError:
        return False


def cluster_up() -> bool:
    if os.environ.get("SDE_E2E_CLUSTER") == "0":
        return False
    return _reachable(PG_ADMIN, 5432) and _reachable(REDIS, 6379) and _reachable(S3, 9000)


BACKENDS = [pytest.param("sqlite", id="sqlite")]
BACKENDS.append(pytest.param("cluster", id="cluster", marks=pytest.mark.skipif(
    not cluster_up(), reason="compose data services not reachable (make deps-up)")))


# --- throwaway cluster resources -------------------------------------------------


class _ClusterResources:
    def __init__(self):
        self.name = f"sde_e2e_{uuid.uuid4().hex[:10]}"

    def _redis(self, db: int):
        import redis

        return redis.Redis.from_url(f"{REDIS.rstrip('/')}/{db}")

    def __enter__(self) -> dict[str, str]:
        import psycopg

        # Reserve a Redis DB for this test alone, so concurrent test sessions on
        # the same cluster can't flush each other's queues.
        reservations = self._redis(0)
        self.redis_db = next((n for n in REDIS_DBS
                              if reservations.set(f"sde-e2e:db{n}", self.name, nx=True, ex=900)), None)
        if self.redis_db is None:
            pytest.skip("every e2e Redis DB is reserved by another test session")
        self._redis(self.redis_db).flushdb()
        with psycopg.connect(PG_ADMIN, autocommit=True) as c:
            c.execute(f'CREATE DATABASE "{self.name}"')
        db_url = urlparse(PG_ADMIN)._replace(path=f"/{self.name}").geturl()
        return {"DATABASE_URL": db_url, "REDIS_URL": f"{REDIS.rstrip('/')}/{self.redis_db}",
                "S3_BUCKET": self.name.replace("_", "-"), "S3_ENDPOINT_URL": S3, **S3_CREDS}

    def __exit__(self, *exc) -> None:
        import boto3
        import psycopg

        with psycopg.connect(PG_ADMIN, autocommit=True) as c:
            c.execute(f'DROP DATABASE IF EXISTS "{self.name}" WITH (FORCE)')
        self._redis(self.redis_db).flushdb()
        reservations = self._redis(0)
        if reservations.get(f"sde-e2e:db{self.redis_db}") == self.name.encode():
            reservations.delete(f"sde-e2e:db{self.redis_db}")
        bucket = boto3.resource("s3", endpoint_url=S3, **{
            "aws_access_key_id": S3_CREDS["AWS_ACCESS_KEY_ID"],
            "aws_secret_access_key": S3_CREDS["AWS_SECRET_ACCESS_KEY"],
            "region_name": S3_CREDS["AWS_DEFAULT_REGION"],
        }).Bucket(self.name.replace("_", "-"))
        try:
            bucket.objects.all().delete()
            bucket.delete()
        except Exception:  # never created, or already gone
            pass


# --- the system under test --------------------------------------------------------


@dataclass
class Pipeline:
    """A test's backend, the operator's API client, and the pipeline processes."""

    kind: str
    tmp: Path
    env: dict[str, str]
    scenario: dict
    procs: list[subprocess.Popen] = field(default_factory=list)
    work_dirs: list[Path] = field(default_factory=list)
    killed: set[str] = field(default_factory=set)  # pids we SIGKILLed

    def __post_init__(self):
        self.backend = from_env({**self.env, "SDE_WORK_DIR": str(self.tmp / "work-api")})
        self.client = TestClient(create_app(self.backend, api_key=API_KEY))
        self.accepted = Path(self.env["SDE_ACCEPTED_DIR"])

    # operator

    def api(self, method: str, path: str, **kw):
        r = self.client.request(method, path, headers={"X-Api-Key": API_KEY}, **kw)
        assert r.status_code < 400, r.text
        return r.json()

    @property
    def task_ids(self) -> list[str]:
        """One task per scenario entry, named so the fakes can find their script."""
        return [f"e2e-{suffix}" for suffix in self.scenario]

    def submit(self) -> list[dict]:
        """Create the scenario's tasks the way a client does: one POST /v1/tasks each."""
        return [self.api("POST", "/v1/tasks", json={"task_id": t}) for t in self.task_ids]

    # pipeline processes ("machines")

    def start(self, machine: str = "m1", step_s: float = 0.05) -> subprocess.Popen:
        """Start one copy of the whole pipeline. Copies on the same `machine`
        share a work dir (a restart); different machines don't (S3 is shared)."""
        work = self.tmp / f"work-{machine}"
        if work not in self.work_dirs:
            self.work_dirs.append(work)
        env = {**os.environ, **BASE_ENV, **self.env, "SDE_WORK_DIR": str(work),
               "SDE_E2E_SCENARIO": json.dumps(self.scenario), "SDE_E2E_STEP_S": str(step_s)}
        log = open(self.tmp / f"stack-{machine}-{len(self.procs)}.log", "w")
        p = subprocess.Popen([sys.executable, str(STACK)], env=env, cwd=ROOT, stdout=log, stderr=log)
        self.procs.append(p)
        return p

    def start_api_server(self) -> tuple[subprocess.Popen, str]:
        """The API as its own process (`python -m api`), on a free port."""
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        env = {**os.environ, **BASE_ENV, **self.env, "SDE_WORK_DIR": str(self.tmp / "work-api"),
               "SDE_API_KEY": API_KEY}
        log = open(self.tmp / "api-server.log", "w")
        p = subprocess.Popen([sys.executable, "-m", "api", "--port", str(port)], env=env, cwd=ROOT,
                             stdout=log, stderr=log)
        self.procs.append(p)
        return p, f"http://127.0.0.1:{port}"

    def run_until_final(self, max_restarts: int = 8, **start_kw) -> int:
        """Keep restarting crashed pipeline processes, as a supervisor would,
        until one exits cleanly. Returns how many crashed."""
        crashes = 0
        while True:
            p = self.start(**start_kw)
            try:
                p.wait(timeout=120)
            except subprocess.TimeoutExpired:
                pytest.fail(f"pipeline did not finish; logs in {self.tmp}")
            if p.returncode == 0:
                return crashes
            crashes += 1
            assert crashes <= max_restarts, f"still crashing after {crashes} restarts; logs in {self.tmp}"

    def kill(self, p: subprocess.Popen) -> None:
        p.send_signal(signal.SIGKILL)
        p.wait()
        self.killed.add(str(p.pid))

    def wait(self, *procs: subprocess.Popen, timeout: float = 120) -> None:
        deadline = time.time() + timeout
        for p in procs:
            try:
                p.wait(timeout=max(1.0, deadline - time.time()))
            except subprocess.TimeoutExpired:
                pytest.fail(f"pipeline did not finish in {timeout}s; logs in {self.tmp}")
            assert p.returncode == 0, f"pipeline exited {p.returncode}; logs in {self.tmp}"

    def wait_until(self, predicate, timeout: float = 60) -> None:
        deadline = time.time() + timeout
        while not predicate():
            if time.time() > deadline:
                pytest.fail(f"condition not reached in {timeout}s")
            time.sleep(0.05)

    # evidence

    def jobs(self) -> list[dict]:
        return [j for t in self.task_ids for j in self.backend.store.task_jobs(t)]

    def settled_count(self) -> int:
        return sum(j["status"] in ("DONE", "DEAD") for j in self.jobs())

    def events(self, kind: str) -> dict[str, Counter]:
        """job id -> Counter of start / finish / adopt, across every machine."""
        return {job: Counter(e for e, _ in evs) for job, evs in self.event_log(kind).items()}

    def event_log(self, kind: str) -> dict[str, list[tuple[str, str]]]:
        """job id -> [(event, pid), ...], across every machine."""
        out: dict[str, list[tuple[str, str]]] = defaultdict(list)
        for work in self.work_dirs:
            for log in (work / "e2e" / kind).glob("*.log"):
                out[log.stem] += [tuple(line.split()[:2]) for line in log.read_text().splitlines()]
        return out

    def pids(self, kind: str) -> set[str]:
        return {line.split()[1] for work in self.work_dirs for log in (work / "e2e" / kind).glob("*.log")
                for line in log.read_text().splitlines()}

    def stop_all(self) -> None:
        for p in self.procs:
            if p.poll() is None:
                p.kill()
                p.wait()


@pytest.fixture(params=BACKENDS)
def make_pipeline(request, tmp_path, monkeypatch):
    """make_pipeline(scenario) -> Pipeline on this test's backend."""
    created: list[Pipeline] = []
    resources = _ClusterResources() if request.param == "cluster" else None
    # SQLite defaults to <SDE_WORK_DIR>/sde.db; every process must share one file.
    env = resources.__enter__() if resources else {"DATABASE_URL": f"sqlite:///{tmp_path / 'sde.db'}"}
    env["SDE_ACCEPTED_DIR"] = str(tmp_path / "accepted")
    # The API (in this process) reads backends and creds from the same settings.
    for k, v in {**BASE_ENV, **env}.items():
        monkeypatch.setenv(k, v)

    def make(scenario: dict) -> Pipeline:
        p = Pipeline(request.param, tmp_path, env, scenario)
        created.append(p)
        return p

    yield make
    for p in created:
        p.stop_all()
    if resources:
        resources.__exit__(None, None, None)
