"""Notification storage for taskmill."""


class NotificationStore:
    """Manages job notifications in the database."""

    def __init__(self, db):
        self.db = db
        self.conn = db.conn

    def get_notification(self, job_id):
        """Return the first notification for a job, or None."""
        row = self.conn.execute(
            "SELECT * FROM notifications WHERE job_id = ? ORDER BY created_at ASC LIMIT 1",
            (job_id,),
        ).fetchone()
        if row is None:
            return None
        return dict(row)

    def notification_exists(self, job_id):
        """Check whether any notification exists for a job."""
        row = self.conn.execute(
            "SELECT 1 FROM notifications WHERE job_id = ?", (job_id,)
        ).fetchone()
        return row is not None

    def count_notifications(self, job_id):
        """Return the number of notification rows for a job."""
        row = self.conn.execute(
            "SELECT COUNT(*) AS cnt FROM notifications WHERE job_id = ?", (job_id,)
        ).fetchone()
        return row["cnt"]

    def list_notifications(self, job_id=None):
        """List notifications, optionally filtered by job_id."""
        if job_id is not None:
            rows = self.conn.execute(
                "SELECT * FROM notifications WHERE job_id = ? ORDER BY created_at",
                (job_id,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM notifications ORDER BY created_at"
            ).fetchall()
        return [dict(r) for r in rows]
