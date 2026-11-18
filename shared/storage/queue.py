"""The `Queue`: how a worker finds its next job (PRD §5.4).

Postgres (or SQLite) is always the source of truth: a job is only ever claimed by
the compare-and-set in `sql.claim`, whatever queue proposed it.

- `DbQueue` (default): the worker asks the database directly for the best job.
- `RedisQueue` (`REDIS_URL` set): one sorted set per job type, scored by `due_at`,
  proposes job ids. The job rows are the outbox: a new job is added to Redis right
  after its transaction commits, and `published_at` is set. If the process dies in
  between, or Redis loses its data, `reconcile()` (on worker-control) rebuilds
  the sets from the claimable rows.
"""

import time
from typing import Any, Protocol

from shared.storage.sql import SqlStore, utcnow
from shared.contracts.models import MAX_JOB_ATTEMPTS, PRIORITY, JobType


class Queue(Protocol):
    def candidates(self, types: list[JobType]) -> list[str | None]:
        """Job ids to try claiming, best first. `[None]` means "let the SQL pick"."""

    def claimed(self, job_id: str) -> None:
        """We won the claim on `job_id`."""

    def missed(self, job_id: str) -> None:
        """The claim on `job_id` failed (taken, finished, or no free Docker slot)."""

    def publish(self, jobs: list[dict[str, Any]]) -> None:
        """Make newly committed jobs (`id`, `type`, `due_at`) visible to workers."""

    def reconcile(self) -> int:
        """Re-add claimable jobs the queue may have lost. Returns how many."""


class DbQueue:
    """The jobs table is the queue; there is nothing to publish or rebuild."""

    def __init__(self, store: SqlStore):
        self.store = store

    def candidates(self, types: list[JobType]) -> list[str | None]:
        return [None]

    def claimed(self, job_id: str) -> None:
        pass

    def missed(self, job_id: str) -> None:
        pass

    def publish(self, jobs: list[dict[str, Any]]) -> None:
        pass

    def reconcile(self) -> int:
        return 0


class RedisQueue:
    """Single-node Redis sorted sets `sde:<type>`; members are job ids."""

    BATCH = 20  # ids to look at per type per poll

    def __init__(self, store: SqlStore, url: str, prefix: str = "sde:"):
        import redis

        self.store = store
        self.prefix = prefix  # several pipelines (or test runs) can share one Redis
        self.r = redis.Redis.from_url(url, decode_responses=True)

    def _key(self, type: str) -> str:
        return f"{self.prefix}{type}"

    def candidates(self, types: list[JobType]) -> list[str | None]:
        now = time.time()
        ids: list[str | None] = []
        for t in sorted(types, key=lambda t: -PRIORITY[t]):
            ids += self.r.zrangebyscore(self._key(t), "-inf", now, start=0, num=self.BATCH)
        return ids

    def claimed(self, job_id: str) -> None:
        self._remove(job_id)

    def missed(self, job_id: str) -> None:
        # Keep the id only if it is still waiting for a slot; anything finished,
        # dead, over its attempt limit or leased by a live worker leaves the set.
        # (An expired lease is re-added by reconcile.)
        job = self.store.job(job_id)
        task = self.store.task(job["task_id"]) if job else None
        if (job is None or job["status"] != "READY" or job["attempts"] >= MAX_JOB_ATTEMPTS
                or task["status"] in ("ACCEPTED", "REJECTED")):
            self._remove(job_id, job["type"] if job else None)

    def _remove(self, job_id: str, type: str | None = None) -> None:
        types = [type] if type else [str(t) for t in JobType]
        for t in types:
            self.r.zrem(self._key(t), job_id)

    def publish(self, jobs: list[dict[str, Any]]) -> None:
        if not jobs:
            return
        pipe = self.r.pipeline()
        for j in jobs:
            pipe.zadd(self._key(j["type"]), {j["id"]: j["due_at"]})
        pipe.execute()
        marks = ",".join("?" * len(jobs))
        self.store.conn.execute(
            f"UPDATE jobs SET published_at = ? WHERE id IN ({marks})",
            (utcnow(), *(j["id"] for j in jobs)),
        )

    def reconcile(self) -> int:
        """Rebuild: every claimable row is put (back) in its set. ZADD is idempotent."""
        jobs = self.store.claimable_ids()
        self.publish(jobs)
        return len(jobs)


def make_queue(store: SqlStore, redis_url: str | None, prefix: str = "sde:") -> Queue:
    return RedisQueue(store, redis_url, prefix) if redis_url else DbQueue(store)
