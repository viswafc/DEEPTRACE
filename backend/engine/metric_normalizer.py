"""
metric_normalizer.py — DeepTrace MetricNormalizer
Convert raw profiler output dicts into structured ProfileMetrics Pydantic models.
Never raises — always returns valid ProfileMetrics even on partial/missing data.
"""

from __future__ import annotations

import logging
import math
from typing import Any, List, Optional

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pydantic models (shared with the rest of the backend)
# ---------------------------------------------------------------------------


class MemoryPoint(BaseModel):
    """Single point on the memory usage timeline."""

    t: float = Field(..., description="Elapsed time in seconds since execution start")
    bytes: int = Field(..., description="Live heap bytes at this point (Python) or RSS bytes (Java)")


class GCEvent(BaseModel):
    """A single garbage collection pause event."""

    t: float = Field(..., description="Timestamp (seconds since execution start)")
    pause_ms: float = Field(..., description="Duration of the GC pause in milliseconds")
    type: str = Field(..., description="'minor' (young-gen) or 'major' (full) GC")
    generation: Optional[int] = Field(None, description="GC generation (Python only)")


class GCMetrics(BaseModel):
    events: List[GCEvent] = Field(default_factory=list)
    total_pause_ms: float = 0.0
    collections: int = 0


class SyscallEntry(BaseModel):
    calls: int = 0
    errors: int = 0
    usecs_per_call: float = 0.0
    time_pct: float = 0.0


class SyscallMetrics(BaseModel):
    by_type: dict[str, SyscallEntry] = Field(default_factory=dict)
    total_calls: int = 0
    total_errors: int = 0
    user_time_pct: float = 0.0
    kernel_time_pct: float = 0.0


class IOMetrics(BaseModel):
    reads_bytes: int = 0
    writes_bytes: int = 0
    blocked_ms: float = 0.0


class MemoryMetrics(BaseModel):
    timeline: List[MemoryPoint] = Field(default_factory=list)
    peak_mb: float = 0.0
    allocations: int = 0
    total_alloc_bytes: int = 0


class CPUMetrics(BaseModel):
    runtime_ms: float = 0.0
    user_time_s: float = 0.0
    kernel_time_s: float = 0.0


class ProfileMetrics(BaseModel):
    """Complete normalised profiling metrics for one execution."""

    job_id: str
    language: str  # 'python' | 'java'
    memory: MemoryMetrics = Field(default_factory=MemoryMetrics)
    gc: GCMetrics = Field(default_factory=GCMetrics)
    syscalls: SyscallMetrics = Field(default_factory=SyscallMetrics)
    io: IOMetrics = Field(default_factory=IOMetrics)
    cpu: CPUMetrics = Field(default_factory=CPUMetrics)
    # Raw cprofile top-functions table (Python only)
    top_functions: list[dict[str, Any]] = Field(default_factory=list)
    # Any exception raised by user code
    exception: Optional[str] = None


# ---------------------------------------------------------------------------
# MetricNormalizer
# ---------------------------------------------------------------------------


class MetricNormalizer:
    """
    Convert raw profiler output dicts to ``ProfileMetrics`` Pydantic models.

    Usage::

        nm = MetricNormalizer()
        metrics = nm.normalize_python(raw_dict, job_id="abc123")
        metrics = nm.normalize_java(raw_dict, job_id="abc123")
    """

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def normalize_python(self, raw: dict[str, Any], job_id: str) -> ProfileMetrics:
        """
        Convert a raw Python profiling dict (as written by profiler_runner.py)
        into a ``ProfileMetrics`` object.
        """
        try:
            return self._do_normalize_python(raw, job_id)
        except Exception as exc:  # noqa: BLE001
            logger.error("normalize_python failed: %s — returning empty metrics", exc)
            return ProfileMetrics(job_id=job_id, language="python")

    def normalize_java(self, raw: dict[str, Any], job_id: str) -> ProfileMetrics:
        """
        Convert a raw Java profiling dict (as produced by JavaProfiler)
        into a ``ProfileMetrics`` object.
        """
        try:
            return self._do_normalize_java(raw, job_id)
        except Exception as exc:  # noqa: BLE001
            logger.error("normalize_java failed: %s — returning empty metrics", exc)
            return ProfileMetrics(job_id=job_id, language="java")

    # ------------------------------------------------------------------
    # Python normalisation
    # ------------------------------------------------------------------

    def _do_normalize_python(self, raw: dict[str, Any], job_id: str) -> ProfileMetrics:
        # ---- Memory ----
        raw_timeline = raw.get("memory_timeline", []) or []
        mem_points = [
            MemoryPoint(t=p.get("t", 0.0), bytes=int(p.get("bytes", 0)))
            for p in raw_timeline
            if isinstance(p, dict)
        ]

        raw_mem = raw.get("memory", {}) or {}
        peak_bytes = raw_mem.get("peak_bytes", 0) or 0
        # Fallback: compute from timeline
        if not peak_bytes and mem_points:
            peak_bytes = max(p.bytes for p in mem_points)

        peak_mb = peak_bytes / (1024 * 1024)
        total_alloc_bytes = int(raw_mem.get("total_alloc_bytes", 0) or 0)
        total_alloc_count = int(raw_mem.get("total_alloc_count", 0) or 0)

        memory_metrics = MemoryMetrics(
            timeline=mem_points,
            peak_mb=round(peak_mb, 4),
            allocations=total_alloc_count,
            total_alloc_bytes=total_alloc_bytes,
        )

        # ---- GC ----
        raw_gc = raw.get("gc_stats", {}) or {}
        gc_raw_events = raw_gc.get("events", []) or []
        gc_events: list[GCEvent] = []

        # Determine t0 (epoch) from first memory snapshot
        t0 = raw_timeline[0].get("t", 0.0) if raw_timeline else 0.0

        for ev in gc_raw_events:
            if not isinstance(ev, dict):
                continue
            ev_t = float(ev.get("t", 0.0))
            # Convert absolute monotonic t to relative offset
            relative_t = max(ev_t - t0, 0.0) if t0 > 0 else ev_t
            gc_events.append(
                GCEvent(
                    t=round(relative_t, 4),
                    pause_ms=round(float(ev.get("pause_ms", 0.0)), 4),
                    type=ev.get("type", "minor"),
                    generation=ev.get("generation"),
                )
            )

        total_pause_ms = sum(ev.pause_ms for ev in gc_events)
        gc_metrics = GCMetrics(
            events=gc_events,
            total_pause_ms=round(total_pause_ms, 4),
            collections=len(gc_events),
        )

        # ---- CPU / Resource ----
        raw_rusage = raw.get("resource_usage", {}) or {}
        utime = float(raw_rusage.get("utime_s", 0.0))
        stime = float(raw_rusage.get("stime_s", 0.0))
        user_pct = float(raw_rusage.get("user_pct", 0.0))
        kernel_pct = float(raw_rusage.get("kernel_pct", 0.0))

        runtime_ms = float(raw.get("runtime_ms", 0.0) or raw.get("_wall_ms", 0.0))

        cpu_metrics = CPUMetrics(
            runtime_ms=round(runtime_ms, 2),
            user_time_s=round(utime, 6),
            kernel_time_s=round(stime, 6),
        )

        # ---- I/O ----
        ru_inblock = int(raw_rusage.get("ru_inblock", 0) or 0)
        ru_oublock = int(raw_rusage.get("ru_oublock", 0) or 0)
        reads_bytes = ru_inblock * 512
        writes_bytes = ru_oublock * 512

        # Estimate blocked_ms from strace I/O syscalls if available
        blocked_ms = 0.0
        raw_strace = raw.get("strace") or {}
        if raw_strace:
            st_by_type = raw_strace.get("by_type", {}) or {}
            read_entry = st_by_type.get("read", {}) or {}
            write_entry = st_by_type.get("write", {}) or {}
            pread_entry = st_by_type.get("pread64", {}) or {}
            pwrite_entry = st_by_type.get("pwrite64", {}) or {}

            for entry in (read_entry, write_entry, pread_entry, pwrite_entry):
                usecs = entry.get("usecs_per_call", 0) * entry.get("calls", 0)
                blocked_ms += usecs / 1000.0

        io_metrics = IOMetrics(
            reads_bytes=reads_bytes,
            writes_bytes=writes_bytes,
            blocked_ms=round(blocked_ms, 3),
        )

        # ---- Syscalls ----
        syscall_metrics = self._build_syscall_metrics(
            raw_strace, user_pct, kernel_pct
        )

        # ---- cProfile top functions ----
        top_functions = []
        raw_cp = raw.get("cprofile", {}) or {}
        if isinstance(raw_cp, dict):
            top_functions = raw_cp.get("top_functions", []) or []

        return ProfileMetrics(
            job_id=job_id,
            language="python",
            memory=memory_metrics,
            gc=gc_metrics,
            syscalls=syscall_metrics,
            io=io_metrics,
            cpu=cpu_metrics,
            top_functions=top_functions,
            exception=raw.get("exception"),
        )

    # ------------------------------------------------------------------
    # Java normalisation
    # ------------------------------------------------------------------

    def _do_normalize_java(self, raw: dict[str, Any], job_id: str) -> ProfileMetrics:
        # ---- Memory ----
        raw_mem_tl = raw.get("memory_timeline", []) or []
        mem_points: list[MemoryPoint] = []
        for p in raw_mem_tl:
            if isinstance(p, dict):
                rss_mb = float(p.get("rss_mb", 0.0))
                mem_points.append(
                    MemoryPoint(
                        t=round(float(p.get("t", 0.0)), 4),
                        bytes=int(rss_mb * 1024 * 1024),
                    )
                )

        peak_mb = max((p.bytes for p in mem_points), default=0) / (1024 * 1024)

        memory_metrics = MemoryMetrics(
            timeline=mem_points,
            peak_mb=round(peak_mb, 4),
            allocations=0,
            total_alloc_bytes=0,
        )

        # ---- GC ----
        raw_gc_events = raw.get("gc_events", []) or []
        gc_events: list[GCEvent] = []
        for ev in raw_gc_events:
            if not isinstance(ev, dict):
                continue
            gc_events.append(
                GCEvent(
                    t=round(float(ev.get("t", 0.0)), 4),
                    pause_ms=round(float(ev.get("pause_ms", 0.0)), 4),
                    type=ev.get("type", "minor"),
                    generation=None,
                )
            )

        total_pause_ms = sum(ev.pause_ms for ev in gc_events)
        gc_metrics = GCMetrics(
            events=gc_events,
            total_pause_ms=round(total_pause_ms, 4),
            collections=len(gc_events),
        )

        # ---- CPU ----
        runtime_ms = float(raw.get("runtime_ms", 0.0))
        cpu_metrics = CPUMetrics(
            runtime_ms=round(runtime_ms, 2),
            user_time_s=0.0,
            kernel_time_s=0.0,
        )

        # ---- Syscalls ----
        raw_strace = raw.get("strace") or {}
        syscall_metrics = self._build_syscall_metrics(raw_strace, 0.0, 0.0)

        # ---- I/O (from strace if available, else 0) ----
        reads_bytes = 0
        writes_bytes = 0
        blocked_ms = 0.0
        if raw_strace:
            st_by_type = raw_strace.get("by_type", {}) or {}
            for key in ("read", "pread64", "recvfrom", "recvmsg"):
                entry = st_by_type.get(key, {}) or {}
                reads_bytes += entry.get("calls", 0)
            for key in ("write", "pwrite64", "sendto", "sendmsg"):
                entry = st_by_type.get(key, {}) or {}
                writes_bytes += entry.get("calls", 0)
            # Convert syscall counts to rough byte estimate (512 B/op heuristic)
            reads_bytes *= 512
            writes_bytes *= 512

        io_metrics = IOMetrics(
            reads_bytes=reads_bytes,
            writes_bytes=writes_bytes,
            blocked_ms=round(blocked_ms, 3),
        )

        return ProfileMetrics(
            job_id=job_id,
            language="java",
            memory=memory_metrics,
            gc=gc_metrics,
            syscalls=syscall_metrics,
            io=io_metrics,
            cpu=cpu_metrics,
            top_functions=[],
            exception=raw.get("stderr") if not raw.get("success") else None,
        )

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_syscall_metrics(
        raw_strace: dict[str, Any],
        user_pct: float,
        kernel_pct: float,
    ) -> SyscallMetrics:
        """Build SyscallMetrics from an optional parsed strace dict."""
        if not raw_strace:
            return SyscallMetrics(
                by_type={},
                total_calls=0,
                total_errors=0,
                user_time_pct=round(user_pct, 2),
                kernel_time_pct=round(kernel_pct, 2),
            )

        by_type_raw = raw_strace.get("by_type", {}) or {}
        by_type: dict[str, SyscallEntry] = {}
        for name, entry in by_type_raw.items():
            if not isinstance(entry, dict):
                continue
            by_type[name] = SyscallEntry(
                calls=int(entry.get("calls", 0)),
                errors=int(entry.get("errors", 0)),
                usecs_per_call=float(entry.get("usecs_per_call", 0.0)),
                time_pct=float(entry.get("time_pct", 0.0)),
            )

        total_calls = int(raw_strace.get("total_calls", 0))
        total_errors = int(raw_strace.get("total_errors", 0))

        # Derive user/kernel split from strace if rusage was not available
        if user_pct == 0.0 and kernel_pct == 0.0 and by_type:
            # Approximate: read/write/select/poll are kernel-side
            kernel_syscalls = {"read", "write", "pread64", "pwrite64",
                               "select", "poll", "epoll_wait", "futex"}
            kernel_calls = sum(
                e.calls for k, e in by_type.items() if k in kernel_syscalls
            )
            if total_calls > 0:
                kernel_pct = round(kernel_calls / total_calls * 100, 2)
                user_pct = round(100.0 - kernel_pct, 2)

        return SyscallMetrics(
            by_type=by_type,
            total_calls=total_calls,
            total_errors=total_errors,
            user_time_pct=round(user_pct, 2),
            kernel_time_pct=round(kernel_pct, 2),
        )
