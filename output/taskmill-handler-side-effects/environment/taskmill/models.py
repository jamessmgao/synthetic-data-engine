"""Data models for taskmill."""

from dataclasses import dataclass
from typing import Optional


@dataclass
class Job:
    """Represents a job in the queue."""

    id: int
    type: str
    payload: str
    status: str  # pending, processing, completed, failed
    worker_id: Optional[str]
    created_at: float
    updated_at: float
