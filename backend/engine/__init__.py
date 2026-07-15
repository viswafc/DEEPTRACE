"""
DeepTrace Engine Package
========================
Provides the full profiling pipeline:

  SandboxExecutor   — safely execute user-submitted code
  PythonProfiler    — cProfile + tracemalloc + GC + strace
  JavaProfiler      — JFR + jstat + psutil + strace
  MetricNormalizer  — convert raw dicts → ProfileMetrics
  BottleneckDetector — analyse ProfileMetrics → List[Bottleneck]

Quick usage example::

    import asyncio
    from engine import SandboxExecutor, MetricNormalizer, BottleneckDetector

    executor   = SandboxExecutor()
    normalizer = MetricNormalizer()
    detector   = BottleneckDetector()

    raw     = await executor.execute_python(code, job_id="demo")
    metrics = normalizer.normalize_python(raw, job_id="demo")
    issues  = detector.detect(metrics, language="python")
"""

from .sandbox import SandboxExecutor
from .profiler_python import PythonProfiler
from .profiler_java import JavaProfiler
from .metric_normalizer import (
    MetricNormalizer,
    ProfileMetrics,
    MemoryMetrics,
    MemoryPoint,
    GCMetrics,
    GCEvent,
    SyscallMetrics,
    SyscallEntry,
    IOMetrics,
    CPUMetrics,
)
from .bottleneck_detector import BottleneckDetector, Bottleneck, Severity

__all__ = [
    # Executors / Profilers
    "SandboxExecutor",
    "PythonProfiler",
    "JavaProfiler",
    # Normalizer + Models
    "MetricNormalizer",
    "ProfileMetrics",
    "MemoryMetrics",
    "MemoryPoint",
    "GCMetrics",
    "GCEvent",
    "SyscallMetrics",
    "SyscallEntry",
    "IOMetrics",
    "CPUMetrics",
    # Bottleneck detection
    "BottleneckDetector",
    "Bottleneck",
    "Severity",
]

__version__ = "1.0.0"
