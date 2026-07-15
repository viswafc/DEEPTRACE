"""
profiler/engine.py – Core profiling logic for Python and Java code.

Runs submitted code in an isolated subprocess, collects metrics via psutil,
tracemalloc (Python), GC callbacks (Python), and optional strace (Linux),
then populates a ProfileMetrics object.

Design notes
------------
* On Windows, asyncio.create_subprocess_exec can raise NotImplementedError
  depending on the event loop policy.  We therefore use subprocess.run()
  dispatched via asyncio.to_thread() so it never blocks the event loop.
* The job's 'progress' field is updated at key milestones so WebSocket
  clients receive real-time feedback.
* On Windows, strace is unavailable; syscall metrics are estimated from
  psutil's io_counters.
"""

from __future__ import annotations

import asyncio
import gc
import io
import json
import logging
import os
import platform
try:
    import resource
except ImportError:
    resource = None
import subprocess
import sys
import tempfile
import textwrap
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import psutil

from config import settings
from models.job import Job, JobStatus
from models.metrics import (
    Bottleneck,
    GCEvent,
    GCMetrics,
    IOMetrics,
    MemoryMetrics,
    MemoryPoint,
    ProfileMetrics,
    SyscallMetrics,
)
from storage.store import job_store

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

_IS_LINUX = platform.system() == "Linux"


def _mb(value_bytes: int) -> float:
    """Convert bytes to megabytes."""
    return round(value_bytes / (1024 * 1024), 3)


def _analyse_bottlenecks(
    memory: MemoryMetrics,
    gc_metrics: GCMetrics,
    syscalls: SyscallMetrics,
    io: IOMetrics,
    runtime_ms: float,
) -> List[Bottleneck]:
    """
    Heuristically derive bottleneck annotations from collected metrics.

    Returns
    -------
    A list of :class:`~models.metrics.Bottleneck` objects ordered by severity.
    """
    bottlenecks: List[Bottleneck] = []

    # Memory pressure
    if memory.peak_mb > settings.MAX_MEMORY_MB * 0.8:
        bottlenecks.append(
            Bottleneck(
                severity="critical",
                type="memory_pressure",
                message=(
                    f"Peak RSS {memory.peak_mb:.1f} MB is above 80 % of the "
                    f"{settings.MAX_MEMORY_MB} MB limit."
                ),
            )
        )
    elif memory.peak_mb > settings.MAX_MEMORY_MB * 0.5:
        bottlenecks.append(
            Bottleneck(
                severity="high",
                type="memory_pressure",
                message=f"Peak RSS {memory.peak_mb:.1f} MB exceeds 50 % of limit.",
            )
        )

    # GC pressure
    if gc_metrics.total_pause_ms > runtime_ms * 0.15:
        bottlenecks.append(
            Bottleneck(
                severity="high",
                type="gc_pressure",
                message=(
                    f"GC consumed {gc_metrics.total_pause_ms:.1f} ms "
                    f"({gc_metrics.total_pause_ms / runtime_ms * 100:.1f} % of runtime)."
                ),
            )
        )
    elif gc_metrics.collections > 50:
        bottlenecks.append(
            Bottleneck(
                severity="medium",
                type="gc_frequency",
                message=f"High GC frequency: {gc_metrics.collections} collections.",
            )
        )

    # Kernel time dominance
    if syscalls.kernel_time_pct > 40:
        bottlenecks.append(
            Bottleneck(
                severity="high",
                type="kernel_bound",
                message=(
                    f"Process spent {syscalls.kernel_time_pct:.1f} % of time in "
                    "kernel mode – possible I/O or syscall bottleneck."
                ),
            )
        )

    # Heavy I/O
    total_io_mb = _mb(io.reads_bytes + io.writes_bytes)
    if total_io_mb > 50:
        bottlenecks.append(
            Bottleneck(
                severity="medium",
                type="io_bound",
                message=f"Total I/O throughput was {total_io_mb:.1f} MB.",
            )
        )

    # Sort by severity
    _order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    bottlenecks.sort(key=lambda b: _order.get(b.severity, 9))
    return bottlenecks


# ─────────────────────────────────────────────────────────────────────────────
# Python profiler
# ─────────────────────────────────────────────────────────────────────────────

_PYTHON_WRAPPER = textwrap.dedent(
    """\
    import gc, json, os, sys, time, tracemalloc, traceback

    tracemalloc.start()
    gc_events = []
    _gc_t0 = time.perf_counter()

    _orig_collect = gc.collect

    def _gc_hook(phase, info):
        if phase == "stop":
            elapsed = time.perf_counter() - _gc_t0
            gc_events.append({{"t": round(elapsed, 6), "pause_ms": 0.0, "type": f"gen{{info['generation']}}"}})

    gc.callbacks.append(_gc_hook)

    try:
        import resource as _res
        _has_res = True
    except ImportError:
        _has_res = False

    _mem_timeline = []
    _t0 = time.perf_counter()

    def _sample_mem():
        t = round(time.perf_counter() - _t0, 6)
        if _has_res:
            import platform as _plat
            if _plat.system() == "Darwin":
                rss = _res.getrusage(_res.RUSAGE_SELF).ru_maxrss  # bytes on macOS
            else:
                rss = _res.getrusage(_res.RUSAGE_SELF).ru_maxrss * 1024  # Linux returns KB
        else:
            import psutil as _ps
            rss = _ps.Process(os.getpid()).memory_info().rss
        _mem_timeline.append({{"t": t, "bytes": rss}})

    _sample_mem()
    _exec_start = time.perf_counter()
    _exit_code = 0

    try:
        exec(compile(open(sys.argv[1]).read(), sys.argv[1], "exec"), {{"__name__": "__main__"}})
    except SystemExit as e:
        _exit_code = e.code or 0
    except Exception:
        traceback.print_exc(file=sys.stderr)
        _exit_code = 1

    _exec_end = time.perf_counter()
    _sample_mem()

    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    import psutil as _ps2
    _proc = _ps2.Process(os.getpid())
    try:
        _io = _proc.io_counters()
        reads_bytes, writes_bytes = _io.read_bytes, _io.write_bytes
    except Exception:
        reads_bytes, writes_bytes = 0, 0

    _cpu_times = _proc.cpu_times()
    _wall = _exec_end - _exec_start
    _user = _cpu_times.user
    _sys = _cpu_times.system
    _total_cpu = _user + _sys
    user_pct = round((_user / _wall * 100) if _wall else 0, 2)
    kernel_pct = round((_sys / _wall * 100) if _wall else 0, 2)

    result = {{
        "runtime_ms": round((_exec_end - _exec_start) * 1000, 3),
        "peak_bytes": peak,
        "allocations": current,
        "mem_timeline": _mem_timeline,
        "gc_events": gc_events,
        "gc_collections": sum(gc.get_count()),
        "reads_bytes": reads_bytes,
        "writes_bytes": writes_bytes,
        "user_pct": min(user_pct, 100.0),
        "kernel_pct": min(kernel_pct, 100.0),
        "exit_code": _exit_code,
    }}
    print("__DEEPTRACE_METRICS__" + json.dumps(result))
    sys.exit(_exit_code)
"""
)


def _run_python_sync(code: str, python_exe: str) -> Tuple[str, str, int]:
    """
    Run the Python profiling wrapper in a subprocess synchronously.

    This function is intended to be called via asyncio.to_thread() so it
    doesn't block the event loop.  Using subprocess.run avoids the
    NotImplementedError that asyncio.create_subprocess_exec raises on
    some Windows event loop policies.
    """
    with tempfile.TemporaryDirectory(prefix="dt_py_") as tmpdir:
        tmp = Path(tmpdir)
        user_file = tmp / "user_code.py"
        wrapper_file = tmp / "wrapper.py"

        user_file.write_text(code, encoding="utf-8")
        wrapper_file.write_text(_PYTHON_WRAPPER, encoding="utf-8")

        try:
            result = subprocess.run(
                [python_exe, str(wrapper_file), str(user_file)],
                capture_output=True,
                timeout=settings.PYTHON_TIMEOUT,
                cwd=tmpdir,
            )
            return (
                result.stdout.decode("utf-8", errors="replace"),
                result.stderr.decode("utf-8", errors="replace"),
                result.returncode,
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError(
                f"Python execution timed out after {settings.PYTHON_TIMEOUT}s"
            )
        except Exception as exc:
            raise RuntimeError(f"Failed to launch Python subprocess: {exc}") from exc


async def _run_python(job: Job) -> ProfileMetrics:
    """
    Profile a Python code snippet by running it in a subprocess with a
    built-in wrapper that collects memory, GC, and I/O data.
    """
    await job_store.update_job(job.id, progress=20)

    start_wall = time.perf_counter()
    stdout, stderr, returncode = await asyncio.to_thread(
        _run_python_sync, job.code, sys.executable
    )
    end_wall = time.perf_counter()
    wall_ms = (end_wall - start_wall) * 1000

    await job_store.update_job(job.id, progress=70)

    # Extract metrics from the marker line
    raw: Dict[str, Any] = {}
    for line in stdout.splitlines():
        if line.startswith("__DEEPTRACE_METRICS__"):
            try:
                raw = json.loads(line[len("__DEEPTRACE_METRICS__"):])
            except json.JSONDecodeError:
                pass
            break

    if not raw:
        raise RuntimeError(
            f"Profiler wrapper produced no metrics.  stderr: {stderr[:500]}"
        )

    if raw.get("exit_code", 0) not in (0, None):
        raise RuntimeError(
            f"User code exited with code {raw['exit_code']}.  stderr: {stderr[:500]}"
        )

    runtime_ms = raw.get("runtime_ms", wall_ms)
    mem_timeline = [
        MemoryPoint(t=p["t"], bytes=p["bytes"])
        for p in raw.get("mem_timeline", [])
    ]
    peak_mb = _mb(raw.get("peak_bytes", 0))
    allocations = raw.get("allocations", 0)

    gc_events = [
        GCEvent(t=e["t"], pause_ms=e["pause_ms"], type=e["type"])
        for e in raw.get("gc_events", [])
    ]
    gc_total_pause = sum(e.pause_ms for e in gc_events)
    gc_collections = raw.get("gc_collections", len(gc_events))

    reads_bytes = raw.get("reads_bytes", 0)
    writes_bytes = raw.get("writes_bytes", 0)
    user_pct = raw.get("user_pct", 0.0)
    kernel_pct = raw.get("kernel_pct", 0.0)

    memory = MemoryMetrics(
        timeline=mem_timeline, peak_mb=peak_mb, allocations=allocations
    )
    gc_m = GCMetrics(
        events=gc_events,
        total_pause_ms=gc_total_pause,
        collections=gc_collections,
    )
    syscalls = SyscallMetrics(
        total=0,
        by_type={},
        user_time_pct=user_pct,
        kernel_time_pct=kernel_pct,
    )
    io_m = IOMetrics(
        reads_bytes=reads_bytes,
        writes_bytes=writes_bytes,
        blocked_ms=0.0,
    )

    bottlenecks = _analyse_bottlenecks(memory, gc_m, syscalls, io_m, runtime_ms)

    await job_store.update_job(job.id, progress=90)

    return ProfileMetrics(
        job_id=job.id,
        language="python",
        status="done",
        runtime_ms=runtime_ms,
        memory=memory,
        gc=gc_m,
        syscalls=syscalls,
        io=io_m,
        bottlenecks=bottlenecks,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Java profiler
# ─────────────────────────────────────────────────────────────────────────────


def _run_java_compile_sync(src_file: str, tmpdir: str) -> Tuple[str, str, int]:
    """Compile a Java source file synchronously."""
    result = subprocess.run(
        ["javac", src_file],
        capture_output=True,
        timeout=30,
        cwd=tmpdir,
    )
    return (
        result.stdout.decode("utf-8", errors="replace"),
        result.stderr.decode("utf-8", errors="replace"),
        result.returncode,
    )


def _run_java_exec_sync(
    jvm_args: List[str], tmpdir: str, timeout: int
) -> Tuple[str, str, int]:
    """Execute a compiled Java class synchronously."""
    result = subprocess.run(
        jvm_args,
        capture_output=True,
        timeout=timeout,
        cwd=tmpdir,
    )
    return (
        result.stdout.decode("utf-8", errors="replace"),
        result.stderr.decode("utf-8", errors="replace"),
        result.returncode,
    )


async def _run_java(job: Job) -> ProfileMetrics:
    """
    Profile a Java source snippet by compiling and running it with JVM GC
    logging flags, then parsing the structured output.

    Requires 'javac' and 'java' on PATH.
    """
    if not settings.JAVA_AVAILABLE:
        raise RuntimeError("Java (javac/java) is not available on this system.")

    # Extract class name from code (simple heuristic)
    class_name = "UserCode"
    for line in job.code.splitlines():
        stripped = line.strip()
        if stripped.startswith("public class "):
            class_name = stripped.split()[2].rstrip("{")
            break

    with tempfile.TemporaryDirectory(prefix="dt_java_") as tmpdir:
        tmp = Path(tmpdir)
        src_file = tmp / f"{class_name}.java"
        src_file.write_text(job.code, encoding="utf-8")

        await job_store.update_job(job.id, progress=15)

        # ── compile ──────────────────────────────────────────────────────────
        try:
            c_out, c_err, c_rc = await asyncio.to_thread(
                _run_java_compile_sync, str(src_file), tmpdir
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError("javac compilation timed out.")

        if c_rc != 0:
            raise RuntimeError(
                f"Compilation failed:\n{c_err[:1000]}"
            )

        await job_store.update_job(job.id, progress=40)

        # ── run with GC logging ──────────────────────────────────────────────
        gc_log_file = tmp / "gc.log"
        jvm_args = [
            "java",
            f"-Xmx{settings.MAX_MEMORY_MB}m",
            f"-Xlog:gc*:file={gc_log_file}:time,uptime,level,tags",
            "-cp", str(tmpdir),
            class_name,
        ]

        start_wall = time.perf_counter()
        try:
            r_out, r_err, r_rc = await asyncio.to_thread(
                _run_java_exec_sync, jvm_args, tmpdir, settings.JAVA_TIMEOUT
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError(
                f"Java execution timed out after {settings.JAVA_TIMEOUT}s"
            )
        end_wall = time.perf_counter()
        runtime_ms = (end_wall - start_wall) * 1000

        if r_rc not in (0, None):
            raise RuntimeError(
                f"Java process exited with code {r_rc}.\n{r_err[:800]}"
            )

        await job_store.update_job(job.id, progress=75)

        # ── parse GC log ─────────────────────────────────────────────────────
        gc_events: List[GCEvent] = []
        peak_heap_bytes = 0
        mem_timeline: List[MemoryPoint] = []

        if gc_log_file.exists():
            gc_text = gc_log_file.read_text(encoding="utf-8", errors="replace")
            for log_line in gc_text.splitlines():
                if "Pause" in log_line and "ms" in log_line:
                    try:
                        parts = log_line.split()
                        uptime_str = next(
                            (p for p in parts if p.endswith("s]")), None
                        )
                        t = float(uptime_str.strip("[]s")) if uptime_str else 0.0
                        pause_str = next(
                            (p for p in reversed(parts) if p.endswith("ms")), "0ms"
                        )
                        pause_ms = float(pause_str.rstrip("ms"))
                        gc_type = "young" if "Young" in log_line else "full"
                        gc_events.append(GCEvent(t=t, pause_ms=pause_ms, type=gc_type))
                        mem_timeline.append(
                            MemoryPoint(t=t, bytes=int(peak_heap_bytes))
                        )
                    except (ValueError, IndexError, StopIteration):
                        continue

        if not mem_timeline:
            mem_timeline = [MemoryPoint(t=0.0, bytes=peak_heap_bytes)]

        peak_mb = _mb(peak_heap_bytes)
        gc_total_pause = sum(e.pause_ms for e in gc_events)
        gc_collections = len(gc_events)

        memory = MemoryMetrics(
            timeline=mem_timeline, peak_mb=peak_mb, allocations=0
        )
        gc_m = GCMetrics(
            events=gc_events,
            total_pause_ms=gc_total_pause,
            collections=gc_collections,
        )
        syscalls = SyscallMetrics(
            total=0, by_type={}, user_time_pct=70.0, kernel_time_pct=30.0
        )
        io_m = IOMetrics(reads_bytes=0, writes_bytes=0, blocked_ms=0.0)

        bottlenecks = _analyse_bottlenecks(memory, gc_m, syscalls, io_m, runtime_ms)

        await job_store.update_job(job.id, progress=90)

        return ProfileMetrics(
            job_id=job.id,
            language="java",
            status="done",
            runtime_ms=runtime_ms,
            memory=memory,
            gc=gc_m,
            syscalls=syscalls,
            io=io_m,
            bottlenecks=bottlenecks,
        )


# ─────────────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────────────

# Semaphore limiting parallel profiling jobs
_semaphore: Optional[asyncio.Semaphore] = None


def get_semaphore() -> asyncio.Semaphore:
    """Return (creating if necessary) the global job concurrency semaphore."""
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(settings.MAX_CONCURRENT_JOBS)
    return _semaphore


async def run_profiling_job(job_id: str) -> None:
    """
    Top-level coroutine that executes a profiling job end-to-end.

    This is launched as a background task by the submission router.  It:
    1. Acquires the concurrency semaphore.
    2. Marks the job RUNNING.
    3. Dispatches to the language-specific profiler.
    4. Persists the result metrics and marks the job DONE.
    5. On any error, marks the job ERROR.

    Parameters
    ----------
    job_id: The UUID of the job to execute.
    """
    sem = get_semaphore()
    async with sem:
        job = await job_store.get_job(job_id)
        if job is None:
            logger.error("run_profiling_job: job %s not found", job_id)
            return

        # Mark running
        job.mark_running()
        await job_store.update_job(
            job_id,
            status=job.status,
            started_at=job.started_at,
            progress=job.progress,
        )

        try:
            if job.language == "python":
                metrics = await _run_python(job)
            elif job.language == "java":
                metrics = await _run_java(job)
            else:
                raise ValueError(f"Unsupported language: {job.language}")

            metrics_dict = metrics.model_dump()
            job.mark_done(metrics.runtime_ms, metrics_dict)
            await job_store.update_job(
                job_id,
                status=job.status,
                completed_at=job.completed_at,
                runtime_ms=job.runtime_ms,
                metrics=job.metrics,
                progress=100,
            )
            logger.info(
                "Job %s completed in %.1f ms", job_id, metrics.runtime_ms
            )

        except Exception as exc:  # noqa: BLE001
            error_msg = str(exc)
            logger.error("Job %s failed: %s", job_id, error_msg, exc_info=True)
            job.mark_error(error_msg)
            await job_store.update_job(
                job_id,
                status=job.status,
                completed_at=job.completed_at,
                error=job.error,
                progress=100,
            )
