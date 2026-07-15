"""
bottleneck_detector.py — DeepTrace BottleneckDetector
Analyse normalised ProfileMetrics and flag actionable performance issues.
"""

from __future__ import annotations

import logging
from enum import Enum
from typing import List

from pydantic import BaseModel

# Import ProfileMetrics from the normalizer; allow standalone usage too.
try:
    from metric_normalizer import ProfileMetrics
except ImportError:
    try:
        from backend.engine.metric_normalizer import ProfileMetrics  # type: ignore[no-redef]
    except ImportError:
        ProfileMetrics = None  # type: ignore[assignment,misc]

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants — tunable thresholds
# ---------------------------------------------------------------------------

# Python
_PY_GC_COLLECTION_WARN: int = 10
_PY_GC_PAUSE_WARN_MS: float = 100.0
_PY_MEMORY_GROWTH_FACTOR: float = 2.0
_PY_HIGH_ALLOC_COUNT: int = 100_000
_PY_KERNEL_OVERHEAD_PCT: float = 40.0
_PY_IO_BOUND_BYTES: int = 10 * 1024 * 1024  # 10 MB
_PY_SLOW_EXEC_MS: float = 10_000.0

# Java
_JAVA_GC_COLLECTION_WARN: int = 5
_JAVA_GC_PAUSE_WARN_MS: float = 200.0
_JAVA_HEAP_PRESSURE_MB: float = 200.0
_JAVA_THREAD_OVERHEAD_PCT: float = 50.0
_JAVA_MINOR_GC_THRASH: int = 20

# Common
_COMMON_IO_SYSCALL_WARN: int = 10_000


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class Severity(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    INFO = "info"


_SEVERITY_ORDER = {Severity.HIGH: 0, Severity.MEDIUM: 1, Severity.INFO: 2}


class Bottleneck(BaseModel):
    """A single detected performance issue."""

    rule: str
    severity: Severity
    message: str
    detail: str = ""


# ---------------------------------------------------------------------------
# BottleneckDetector
# ---------------------------------------------------------------------------


class BottleneckDetector:
    """
    Analyse a ``ProfileMetrics`` object and return a list of ``Bottleneck``
    findings sorted by severity (HIGH first).

    Usage::

        detector = BottleneckDetector()
        issues = detector.detect(metrics, language="python")
    """

    def detect(
        self, metrics: "ProfileMetrics", language: str
    ) -> List[Bottleneck]:
        """
        Run all applicable detection rules.

        Parameters
        ----------
        metrics:
            Normalised profiling data from ``MetricNormalizer``.
        language:
            ``'python'`` or ``'java'``.

        Returns
        -------
        List[Bottleneck]
            Findings sorted by severity (HIGH → MEDIUM → INFO).
        """
        if metrics is None:
            logger.warning("detect() called with None metrics — returning empty list")
            return []

        findings: List[Bottleneck] = []

        lang = language.lower().strip()

        # Language-specific rules
        if lang == "python":
            findings.extend(self._rules_python(metrics))
        elif lang == "java":
            findings.extend(self._rules_java(metrics))
        else:
            logger.warning("Unknown language '%s' — skipping language-specific rules", lang)

        # Common rules (applicable to all languages)
        findings.extend(self._rules_common(metrics))

        # Sort: HIGH first, then MEDIUM, then INFO; stable within same severity
        findings.sort(key=lambda b: _SEVERITY_ORDER.get(b.severity, 99))

        return findings

    # ------------------------------------------------------------------
    # Python-specific rules
    # ------------------------------------------------------------------

    def _rules_python(self, m: "ProfileMetrics") -> List[Bottleneck]:
        findings: List[Bottleneck] = []

        gc = m.gc
        memory = m.memory
        syscalls = m.syscalls
        io = m.io
        cpu = m.cpu

        # ── gc_pressure ──────────────────────────────────────────────────
        if gc.collections > _PY_GC_COLLECTION_WARN and gc.total_pause_ms > _PY_GC_PAUSE_WARN_MS:
            findings.append(
                Bottleneck(
                    rule="gc_pressure",
                    severity=Severity.HIGH,
                    message=(
                        f"Excessive GC activity: {gc.collections} collections "
                        f"totalling {gc.total_pause_ms:.1f} ms of pause time."
                    ),
                    detail=(
                        "High GC pressure often indicates excessive short-lived object creation. "
                        "Consider object pooling, using generators instead of lists, "
                        "or pre-allocating data structures. "
                        f"(Threshold: >{_PY_GC_COLLECTION_WARN} collections AND "
                        f">{_PY_GC_PAUSE_WARN_MS} ms total pause.)"
                    ),
                )
            )

        # ── memory_leak ───────────────────────────────────────────────────
        if len(memory.timeline) >= 2:
            first_bytes = memory.timeline[0].bytes
            last_bytes = memory.timeline[-1].bytes
            if first_bytes > 0 and last_bytes > first_bytes * _PY_MEMORY_GROWTH_FACTOR:
                growth_factor = last_bytes / first_bytes
                findings.append(
                    Bottleneck(
                        rule="memory_leak",
                        severity=Severity.MEDIUM,
                        message=(
                            f"Memory grew {growth_factor:.1f}× during execution "
                            f"({first_bytes // 1024} KB → {last_bytes // 1024} KB)."
                        ),
                        detail=(
                            "Monotonically growing memory often indicates a memory leak: "
                            "unbounded caches, global state accumulation, or circular references "
                            "preventing garbage collection. Use tracemalloc top-allocation data "
                            "to identify the culprit. "
                            f"(Threshold: end > {_PY_MEMORY_GROWTH_FACTOR}× start.)"
                        ),
                    )
                )

        # ── high_alloc_rate ───────────────────────────────────────────────
        if memory.allocations > _PY_HIGH_ALLOC_COUNT:
            findings.append(
                Bottleneck(
                    rule="high_alloc_rate",
                    severity=Severity.MEDIUM,
                    message=(
                        f"High allocation count: {memory.allocations:,} live objects "
                        f"({memory.total_alloc_bytes // 1024:,} KB total)."
                    ),
                    detail=(
                        "Large numbers of small allocations increase GC pressure and "
                        "can fragment the heap. Consider using NumPy arrays, struct, "
                        "or __slots__ on frequently-instantiated classes. "
                        f"(Threshold: >{_PY_HIGH_ALLOC_COUNT:,} live allocations.)"
                    ),
                )
            )

        # ── kernel_overhead ───────────────────────────────────────────────
        if syscalls.kernel_time_pct > _PY_KERNEL_OVERHEAD_PCT:
            findings.append(
                Bottleneck(
                    rule="kernel_overhead",
                    severity=Severity.HIGH,
                    message=(
                        f"High kernel time: {syscalls.kernel_time_pct:.1f}% of CPU "
                        "spent in kernel mode."
                    ),
                    detail=(
                        "Spending more than 40 % of CPU time in kernel mode suggests "
                        "excessive system calls (I/O, mmap, futex). "
                        "Batch reads/writes, avoid synchronous disk I/O in hot paths, "
                        "and reduce lock contention. "
                        f"(Threshold: >{_PY_KERNEL_OVERHEAD_PCT}% kernel time.)"
                    ),
                )
            )

        # ── io_bound ─────────────────────────────────────────────────────
        total_io_bytes = io.reads_bytes + io.writes_bytes
        if total_io_bytes > _PY_IO_BOUND_BYTES:
            findings.append(
                Bottleneck(
                    rule="io_bound",
                    severity=Severity.MEDIUM,
                    message=(
                        f"I/O-bound execution: {total_io_bytes // (1024 * 1024):.1f} MB "
                        "of block I/O detected."
                    ),
                    detail=(
                        "Large amounts of block I/O can severely limit throughput. "
                        "Consider buffering, memory-mapped files (mmap), async I/O, "
                        "or caching frequently-read data. "
                        f"(Threshold: >{_PY_IO_BOUND_BYTES // (1024 * 1024)} MB I/O.)"
                    ),
                )
            )

        # ── slow_execution ────────────────────────────────────────────────
        if cpu.runtime_ms > _PY_SLOW_EXEC_MS:
            findings.append(
                Bottleneck(
                    rule="slow_execution",
                    severity=Severity.INFO,
                    message=(
                        f"Long execution time: {cpu.runtime_ms / 1000:.2f}s "
                        "(wall clock)."
                    ),
                    detail=(
                        "Execution time exceeds 10 seconds. Examine the cProfile "
                        "top-functions table to locate hotspots. Consider algorithmic "
                        "improvements, Cython, NumPy vectorisation, or multiprocessing "
                        "for CPU-bound workloads. "
                        f"(Threshold: >{_PY_SLOW_EXEC_MS / 1000:.0f}s runtime.)"
                    ),
                )
            )

        return findings

    # ------------------------------------------------------------------
    # Java-specific rules
    # ------------------------------------------------------------------

    def _rules_java(self, m: "ProfileMetrics") -> List[Bottleneck]:
        findings: List[Bottleneck] = []

        gc = m.gc
        memory = m.memory
        syscalls = m.syscalls
        cpu = m.cpu

        # ── full_gc_detected ──────────────────────────────────────────────
        major_events = [ev for ev in gc.events if ev.type == "major"]
        if major_events:
            total_major_ms = sum(ev.pause_ms for ev in major_events)
            findings.append(
                Bottleneck(
                    rule="full_gc_detected",
                    severity=Severity.HIGH,
                    message=(
                        f"Full GC (stop-the-world) detected: {len(major_events)} "
                        f"full collection(s), {total_major_ms:.1f} ms total pause."
                    ),
                    detail=(
                        "Full GCs cause application-wide stop-the-world pauses and "
                        "indicate that the old generation is being exhausted. "
                        "Increase heap size (-Xmx), reduce object promotion by "
                        "shortening object lifetimes, or switch to a low-pause collector "
                        "such as G1GC or ZGC (-XX:+UseZGC)."
                    ),
                )
            )

        # ── gc_pressure ───────────────────────────────────────────────────
        if gc.collections > _JAVA_GC_COLLECTION_WARN and gc.total_pause_ms > _JAVA_GC_PAUSE_WARN_MS:
            findings.append(
                Bottleneck(
                    rule="gc_pressure",
                    severity=Severity.HIGH,
                    message=(
                        f"Java GC pressure: {gc.collections} collections, "
                        f"{gc.total_pause_ms:.1f} ms total pause time."
                    ),
                    detail=(
                        "Frequent GC cycles indicate high allocation pressure. "
                        "Profile heap allocation with async-profiler or JFR to find "
                        "allocation hotspots. Reuse objects via object pools, use "
                        "primitive arrays instead of boxed types, and avoid finalizers. "
                        f"(Threshold: >{_JAVA_GC_COLLECTION_WARN} collections AND "
                        f">{_JAVA_GC_PAUSE_WARN_MS} ms pause.)"
                    ),
                )
            )

        # ── heap_pressure ─────────────────────────────────────────────────
        if memory.peak_mb > _JAVA_HEAP_PRESSURE_MB:
            findings.append(
                Bottleneck(
                    rule="heap_pressure",
                    severity=Severity.MEDIUM,
                    message=f"High heap usage: peak RSS {memory.peak_mb:.1f} MB.",
                    detail=(
                        "Peak memory usage exceeds 200 MB. Investigate large data "
                        "structures, off-heap leaks, and retained object graphs using "
                        "a heap dump (jmap -dump) and MAT or VisualVM. "
                        f"(Threshold: >{_JAVA_HEAP_PRESSURE_MB} MB peak RSS.)"
                    ),
                )
            )

        # ── thread_overhead ───────────────────────────────────────────────
        if syscalls.kernel_time_pct > _JAVA_THREAD_OVERHEAD_PCT:
            findings.append(
                Bottleneck(
                    rule="thread_overhead",
                    severity=Severity.MEDIUM,
                    message=(
                        f"High kernel time: {syscalls.kernel_time_pct:.1f}% — "
                        "possible thread synchronisation overhead."
                    ),
                    detail=(
                        "Spending more than 50% of CPU time in kernel mode on a JVM "
                        "workload often indicates excessive lock contention, thread "
                        "context switching, or futex waits. "
                        "Use jstack thread dumps or JFR lock profiling to identify "
                        "contested monitors. "
                        f"(Threshold: >{_JAVA_THREAD_OVERHEAD_PCT}% kernel time.)"
                    ),
                )
            )

        # ── memory_fragmentation (minor GC thrashing) ─────────────────────
        minor_events = [ev for ev in gc.events if ev.type == "minor"]
        if len(minor_events) > _JAVA_MINOR_GC_THRASH:
            findings.append(
                Bottleneck(
                    rule="memory_fragmentation",
                    severity=Severity.HIGH,
                    message=(
                        f"Minor GC thrashing: {len(minor_events)} young-generation "
                        "collections detected."
                    ),
                    detail=(
                        "Extremely frequent minor GCs indicate that the young generation "
                        "is too small or that the allocation rate is unsustainable. "
                        "Increase the young-gen size (-Xmn or -XX:NewRatio), reduce "
                        "allocation rate, or switch to G1GC for automatic region sizing. "
                        f"(Threshold: >{_JAVA_MINOR_GC_THRASH} minor GCs.)"
                    ),
                )
            )

        return findings

    # ------------------------------------------------------------------
    # Common rules
    # ------------------------------------------------------------------

    def _rules_common(self, m: "ProfileMetrics") -> List[Bottleneck]:
        findings: List[Bottleneck] = []

        syscalls = m.syscalls

        # ── excessive_io_syscalls ─────────────────────────────────────────
        read_calls = 0
        write_calls = 0
        for syscall_name, entry in syscalls.by_type.items():
            if "read" in syscall_name:
                read_calls += entry.calls
            elif "write" in syscall_name:
                write_calls += entry.calls

        total_io_syscalls = read_calls + write_calls
        if total_io_syscalls > _COMMON_IO_SYSCALL_WARN:
            findings.append(
                Bottleneck(
                    rule="excessive_io",
                    severity=Severity.MEDIUM,
                    message=(
                        f"Excessive I/O syscalls: {total_io_syscalls:,} "
                        f"({read_calls:,} reads + {write_calls:,} writes)."
                    ),
                    detail=(
                        "A very large number of read/write system calls indicates "
                        "unbuffered or fine-grained I/O. Use larger buffers, "
                        "batch writes with BufferedWriter/BufferedOutputStream (Java) "
                        "or io.BufferedWriter (Python), and reduce flush frequency. "
                        f"(Threshold: >{_COMMON_IO_SYSCALL_WARN:,} I/O-related syscalls.)"
                    ),
                )
            )

        return findings


# ---------------------------------------------------------------------------
# CLI helper
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import json
    import sys

    if len(sys.argv) < 3:
        print("Usage: bottleneck_detector.py <metrics_json_file> <language>")
        sys.exit(1)

    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "metric_normalizer",
        str(__import__("pathlib").Path(__file__).parent / "metric_normalizer.py"),
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    _ProfileMetrics = mod.ProfileMetrics

    with open(sys.argv[1]) as fh:
        data = json.load(fh)

    _metrics = _ProfileMetrics.model_validate(data)
    _detector = BottleneckDetector()
    _issues = _detector.detect(_metrics, sys.argv[2])
    print(json.dumps([b.model_dump() for b in _issues], indent=2))
