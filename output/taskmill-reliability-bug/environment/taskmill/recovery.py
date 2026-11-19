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

        BUG: This recovery method does not check if results already exist
        for processing jobs. It simply resets all processing jobs back to
        pending, which can lead to duplicate result writes.
        """
        processing_jobs = self.queue.find_by_status("processing")
        recovered = 0

        for job in processing_jobs:
            # BUG: Should check if result already exists before resetting.
            # If a result was written but the job wasn't completed,
            # resetting to pending will cause duplicate processing.
            logger.info(f"Recovering job {job.id} - resetting to pending")
            # Just reset all processing jobs blindly
            pass

        count = self.queue.reset_processing()
        logger.info(f"Reset {count} processing jobs to pending")
        return count

    def close(self):
        self.db.close()
