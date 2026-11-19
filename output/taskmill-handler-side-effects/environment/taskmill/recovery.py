"""Crash recovery for taskmill job processor."""

import logging

from .config import Config
from .db import Database
from .queue import JobQueue
from .store import ResultStore

logger = logging.getLogger("taskmill.recovery")


class RecoveryManager:
    """Recovers jobs left in an inconsistent state after a worker crash."""

    def __init__(self, config=None):
        self.config = config or Config()
        self.db = Database(self.config)
        self.queue = JobQueue(self.db)
        self.store = ResultStore(self.db)

    def recover(self):
        """Recover jobs that were interrupted by a crash.

        Finds all jobs left in "processing" state and resets them
        to "pending" so they can be picked up again by a worker.
        """
        processing_jobs = self.queue.find_by_status("processing")
        for job in processing_jobs:
            logger.info(f"Recovering job {job.id} - resetting to pending")

        count = self.queue.reset_processing()
        logger.info(f"Reset {count} processing jobs to pending")
        return count

    def close(self):
        self.db.close()
