"""Custom exceptions for taskmill."""


class TaskmillError(Exception):
    """Base exception for taskmill."""


class JobNotFoundError(TaskmillError):
    """Raised when a job is not found or not in an expected state."""


class ResultExistsError(TaskmillError):
    """Raised when a result already exists for a job."""
