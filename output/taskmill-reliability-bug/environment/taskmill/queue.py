"""Job queue operations for taskmill."""

import time
import uuid

from .exceptions import JobNotFoundError
from .models import Job


class JobQueue:
    """Manages job state transitions in the database."""

    def __init__(self, db):
        self.db = db
        self.conn = db.conn

    def enqueue(self, job_type, payload):
        """Add a new job to the queue and return its ID."""
        now = time.time()
        with self.conn:
            cursor = self.conn.execute(
                "INSERT INTO jobs (type, payload, status, created_at, updated_at) "
                "VALUES (?, ?, 'pending', ?, ?)",
                (job_type, payload, now, now),
            )
            return cursor.lastrowid

    def dequeue(self, worker_id=None):
        """Pick the next pending job and set it to processing."""
        worker_id = worker_id or str(uuid.uuid4())[:8]
        now = time.time()
        with self.conn:
            row = self.conn.execute(
                "SELECT * FROM jobs WHERE status = 'pending' ORDER BY created_at ASC LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            self.conn.execute(
                "UPDATE jobs SET status = 'processing', worker_id = ?, updated_at = ? "
                "WHERE id = ?",
                (worker_id, now, row["id"]),
            )
            return Job(
                id=row["id"],
                type=row["type"],
                payload=row["payload"],
                status="processing",
                worker_id=worker_id,
                created_at=row["created_at"],
                updated_at=now,
            )

    def complete_job(self, job_id):
        """Mark a processing job as completed."""
        now = time.time()
        with self.conn:
            cursor = self.conn.execute(
                "UPDATE jobs SET status = 'completed', updated_at = ? "
                "WHERE id = ? AND status = 'processing'",
                (now, job_id),
            )
            if cursor.rowcount == 0:
                raise JobNotFoundError(
                    f"Job {job_id} not found or not in processing state"
                )

    def fail_job(self, job_id):
        """Mark a job as failed."""
        now = time.time()
        with self.conn:
            self.conn.execute(
                "UPDATE jobs SET status = 'failed', updated_at = ? WHERE id = ?",
                (now, job_id),
            )

    def find_by_status(self, status):
        """Return all jobs with the given status."""
        rows = self.conn.execute(
            "SELECT * FROM jobs WHERE status = ? ORDER BY created_at ASC",
            (status,),
        ).fetchall()
        return [
            Job(
                id=r["id"],
                type=r["type"],
                payload=r["payload"],
                status=r["status"],
                worker_id=r["worker_id"],
                created_at=r["created_at"],
                updated_at=r["updated_at"],
            )
            for r in rows
        ]

    def get_job(self, job_id):
        """Return a single job by ID."""
        row = self.conn.execute(
            "SELECT * FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
        if row is None:
            raise JobNotFoundError(f"Job {job_id} not found")
        return Job(
            id=row["id"],
            type=row["type"],
            payload=row["payload"],
            status=row["status"],
            worker_id=row["worker_id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def reset_processing(self):
        """Reset all processing jobs back to pending for recovery."""
        now = time.time()
        with self.conn:
            cursor = self.conn.execute(
                "UPDATE jobs SET status = 'pending', worker_id = NULL, updated_at = ? "
                "WHERE status = 'processing'",
                (now,),
            )
            return cursor.rowcount
