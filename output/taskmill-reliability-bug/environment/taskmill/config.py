"""Configuration for taskmill."""

import os


class Config:
    """Application configuration read from environment variables."""

    def __init__(self, db_path=None, output_dir=None):
        self.db_path = db_path or os.environ.get(
            "TASKMILL_DB", "/var/lib/taskmill/taskmill.db"
        )
        self.output_dir = output_dir or os.environ.get(
            "TASKMILL_OUTPUT", "/var/lib/taskmill/output"
        )
        # Diagnostic crash-injection flags (job-id or empty)
        self.crash_after_complete = os.environ.get("TASKMILL_CRASH_AFTER_COMPLETE", "")
        self.crash_after_result = os.environ.get("TASKMILL_CRASH_AFTER_RESULT", "")
