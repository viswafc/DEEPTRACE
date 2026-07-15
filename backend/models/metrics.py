"""
models/metrics.py – Pydantic schema for DeepTrace profiling metrics.

These models represent the complete structured output produced by the profiler
engines and returned to clients via the /api/results and /api/compare endpoints.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Low-level building blocks
# ---------------------------------------------------------------------------


class MemoryPoint(BaseModel):
    """A single sample in the RSS memory timeline.

    Attributes
    ----------
    t:      Elapsed time (seconds) from job start.
    bytes:  Resident Set Size at sample time, in bytes.
    """

    t: float = Field(..., description="Elapsed seconds since job start.")
    bytes: int = Field(..., ge=0, description="RSS memory in bytes.")


class GCEvent(BaseModel):
    """Represents one garbage-collection pause.

    Attributes
    ----------
    t:         Elapsed time (seconds) when the GC pause began.
    pause_ms:  Duration of the pause in milliseconds.
    type:      GC generation / collection type label (e.g. 'gen0', 'full').
    """

    t: float = Field(..., description="Elapsed seconds when GC pause began.")
    pause_ms: float = Field(..., ge=0, description="Pause duration in milliseconds.")
    type: str = Field(..., description="GC collection type label.")


# ---------------------------------------------------------------------------
# Per-category metric containers
# ---------------------------------------------------------------------------


class MemoryMetrics(BaseModel):
    """Aggregated memory usage information for a profiling run.

    Attributes
    ----------
    timeline:    Ordered list of (t, bytes) RSS samples.
    peak_mb:     Maximum RSS observed during the run, in megabytes.
    allocations: Estimated total object allocations (Python tracemalloc count).
    """

    timeline: List[MemoryPoint] = Field(default_factory=list)
    peak_mb: float = Field(..., ge=0, description="Peak RSS in megabytes.")
    allocations: int = Field(..., ge=0, description="Total object allocations.")


class GCMetrics(BaseModel):
    """Garbage-collection statistics for a profiling run.

    Attributes
    ----------
    events:          List of individual GC pause events.
    total_pause_ms:  Sum of all GC pause durations in milliseconds.
    collections:     Total number of GC collection cycles observed.
    """

    events: List[GCEvent] = Field(default_factory=list)
    total_pause_ms: float = Field(..., ge=0)
    collections: int = Field(..., ge=0)


class SyscallMetrics(BaseModel):
    """System-call statistics (populated via strace on Linux, estimated otherwise).

    Attributes
    ----------
    total:            Total number of syscalls issued.
    by_type:          Map of syscall name → invocation count.
    user_time_pct:    Percentage of elapsed time spent in user space.
    kernel_time_pct:  Percentage of elapsed time spent in kernel space.
    """

    total: int = Field(..., ge=0)
    by_type: Dict[str, int] = Field(default_factory=dict)
    user_time_pct: float = Field(..., ge=0, le=100)
    kernel_time_pct: float = Field(..., ge=0, le=100)


class IOMetrics(BaseModel):
    """I/O statistics for a profiling run.

    Attributes
    ----------
    reads_bytes:   Total bytes read from disk / sockets.
    writes_bytes:  Total bytes written to disk / sockets.
    blocked_ms:    Estimated time (ms) the process spent blocked on I/O.
    """

    reads_bytes: int = Field(..., ge=0)
    writes_bytes: int = Field(..., ge=0)
    blocked_ms: float = Field(..., ge=0)


class Bottleneck(BaseModel):
    """A detected performance bottleneck or anomaly.

    Attributes
    ----------
    severity:  'critical', 'high', 'medium', or 'low'.
    type:      Category label, e.g. 'memory_leak', 'gc_pressure', 'io_bound'.
    message:   Human-readable description of the issue.
    line:      Source-code line number associated with the issue, if known.
    """

    severity: str = Field(..., description="Severity level: critical/high/medium/low.")
    type: str = Field(..., description="Bottleneck category label.")
    message: str = Field(..., description="Human-readable description.")
    line: Optional[int] = Field(None, description="Related source line number.")


# ---------------------------------------------------------------------------
# Top-level aggregate
# ---------------------------------------------------------------------------


class ProfileMetrics(BaseModel):
    """Complete profiling result for a single job.

    This is the document stored in Job.metrics and returned from /api/results.

    Attributes
    ----------
    job_id:      The job this result belongs to.
    language:    Language that was profiled.
    status:      'done' or 'error'.
    runtime_ms:  Total wall-clock execution time in milliseconds.
    memory:      Memory usage metrics.
    gc:          Garbage-collection metrics.
    syscalls:    System-call metrics.
    io:          I/O metrics.
    bottlenecks: Automatically detected performance issues, ordered by severity.
    """

    job_id: str
    language: str
    status: str
    runtime_ms: float = Field(..., ge=0)
    memory: MemoryMetrics
    gc: GCMetrics
    syscalls: SyscallMetrics
    io: IOMetrics
    bottlenecks: List[Bottleneck] = Field(default_factory=list)
