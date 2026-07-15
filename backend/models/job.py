"""
models/job.py – Job lifecycle data model for DeepTrace.

Defines the JobStatus enum and the Job Pydantic model that is stored in memory
and persisted as JSON to disk.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Optional

from pydantic import BaseModel, Field


class JobStatus(str, Enum):
    """Lifecycle states of a profiling job."""

    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    ERROR = "error"


class Job(BaseModel):
    """
    Represents a single profiling job submitted by a client.

    Attributes
    ----------
    id:            Unique job identifier (UUID-4 string).
    language:      Target language – 'python' or 'java'.
    code:          Source code submitted for profiling.
    status:        Current lifecycle state.
    created_at:    UTC timestamp when the job was created.
    started_at:    UTC timestamp when execution began (None until then).
    completed_at:  UTC timestamp when execution finished (None until then).
    runtime_ms:    Wall-clock execution time in milliseconds (None until done).
    error:         Human-readable error message when status == ERROR.
    metrics:       Raw metric dictionary populated on completion.
    progress:      Integer progress indicator in the range [0, 100].
    """

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    language: str
    code: str
    status: JobStatus = JobStatus.QUEUED
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    runtime_ms: Optional[float] = None
    error: Optional[str] = None
    metrics: Optional[Dict[str, Any]] = None
    progress: int = Field(default=0, ge=0, le=100)

    model_config = {"use_enum_values": True}

    # ---------------------------------------------------------------- helpers

    def mark_running(self) -> None:
        """Transition the job to RUNNING state and record the start timestamp."""
        self.status = JobStatus.RUNNING
        self.started_at = datetime.now(timezone.utc)
        self.progress = 5

    def mark_done(self, runtime_ms: float, metrics: Dict[str, Any]) -> None:
        """Transition the job to DONE state and persist profiling results."""
        self.status = JobStatus.DONE
        self.completed_at = datetime.now(timezone.utc)
        self.runtime_ms = runtime_ms
        self.metrics = metrics
        self.progress = 100

    def mark_error(self, message: str) -> None:
        """Transition the job to ERROR state and record the failure reason."""
        self.status = JobStatus.ERROR
        self.completed_at = datetime.now(timezone.utc)
        self.error = message
        self.progress = 100

    def set_progress(self, value: int) -> None:
        """Update progress, clamping to [0, 100]."""
        self.progress = max(0, min(100, value))
