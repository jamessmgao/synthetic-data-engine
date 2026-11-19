"""Result storage for taskmill."""

from .exceptions import ResultExistsError


class ResultStore:
    """Manages job results in the database."""

    def __init__(self, db):
        self.db = db
        self.conn = db.conn

    def write_result(self, job_id, output):
        """Write a result for a job."""
        import time

        now = time.time()
        with self.conn:
            self.conn.execute(
                "INSERT INTO results (job_id, output, created_at) VALUES (?, ?, ?)",
                (job_id, output, now),
            )

    def get_result(self, job_id):
        """Return the result for a job, or None."""
        row = self.conn.execute(
            "SELECT * FROM results WHERE job_id = ?", (job_id,)
        ).fetchone()
        if row is None:
            return None
        return dict(row)

    def result_exists(self, job_id):
        """Check whether a result already exists for a job."""
        row = self.conn.execute(
            "SELECT 1 FROM results WHERE job_id = ?", (job_id,)
        ).fetchone()
        return row is not None

    def count_results(self, job_id):
        """Return the number of result rows for a job."""
        row = self.conn.execute(
            "SELECT COUNT(*) AS cnt FROM results WHERE job_id = ?", (job_id,)
        ).fetchone()
        return row["cnt"]
