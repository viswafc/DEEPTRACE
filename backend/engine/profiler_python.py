"""
profiler_python.py — DeepTrace PythonProfiler
Orchestrates multi-tool Python profiling: cProfile, tracemalloc, GC callbacks,
resource.getrusage, optional strace wrapping.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
import re
import shutil
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

IS_LINUX: bool = platform.system() == "Linux"
STRACE_AVAILABLE: bool = IS_LINUX and shutil.which("strace") is not None

JOBS_DIR: Path = Path(os.environ.get("DEEPTRACE_JOBS_DIR", "/tmp/deeptrace_jobs"))
MAX_MEMORY_MB: int = int(os.environ.get("DEEPTRACE_MAX_MEMORY_MB", "512"))
DEFAULT_TIMEOUT: int = int(os.environ.get("DEEPTRACE_TIMEOUT_S", "30"))


# ---------------------------------------------------------------------------
# Profiler runner script (identical to the one embedded in sandbox.py,
# kept here as well so PythonProfiler can operate independently).
# ---------------------------------------------------------------------------

_RUNNER_SCRIPT: str = r'''#!/usr/bin/env python3
"""
profiler_runner.py — DeepTrace Python Profiler Runner
Self-contained script: profile a user code file, write results to
<job_dir>/profiling_output.json.

Usage:
    python profiler_runner.py <user_code_file> <job_dir>
"""

import cProfile
import gc
import io
import json
import pstats
import sys
import threading
import time
import tracemalloc

try:
    import resource as _resource
    _HAS_RESOURCE = True
except ImportError:
    _HAS_RESOURCE = False

# ---------------------------------------------------------------------------
# Args
# ---------------------------------------------------------------------------
if len(sys.argv) < 3:
    print("Usage: profiler_runner.py <user_code_file> <job_dir>", file=sys.stderr)
    sys.exit(1)

user_code_file = sys.argv[1]
job_dir        = sys.argv[2]

with open(user_code_file, "r", encoding="utf-8") as _fh:
    user_code = _fh.read()

# ---------------------------------------------------------------------------
# Tracemalloc
# ---------------------------------------------------------------------------
tracemalloc.start(25)

# ---------------------------------------------------------------------------
# Memory snapshot thread (every 100 ms)
# ---------------------------------------------------------------------------
_mem_snapshots: list[dict] = []
_stop_event = threading.Event()

def _mem_poller():
    while not _stop_event.is_set():
        snap = tracemalloc.take_snapshot()
        stats = snap.statistics("lineno")
        total = sum(s.size for s in stats)
        _mem_snapshots.append({"t": time.monotonic(), "bytes": total})
        _stop_event.wait(timeout=0.1)

_poll_thread = threading.Thread(target=_mem_poller, daemon=True)
_poll_thread.start()

# ---------------------------------------------------------------------------
# GC callbacks
# ---------------------------------------------------------------------------
_gc_events: list[dict] = []
_gc_start_t: float | None = None

def _gc_cb(phase: str, info: dict):
    global _gc_start_t
    if phase == "start":
        _gc_start_t = time.monotonic()
    elif phase == "stop" and _gc_start_t is not None:
        pause_ms = (time.monotonic() - _gc_start_t) * 1000
        gen = info.get("generation", 0)
        _gc_events.append({
            "t":            _gc_start_t,
            "pause_ms":     pause_ms,
            "type":         "major" if gen == 2 else "minor",
            "generation":   gen,
            "collected":    info.get("collected", 0),
            "uncollectable": info.get("uncollectable", 0),
        })
        _gc_start_t = None

gc.callbacks.append(_gc_cb)

# ---------------------------------------------------------------------------
# Resource — before
# ---------------------------------------------------------------------------
_ru_before   = _resource.getrusage(_resource.RUSAGE_SELF) if _HAS_RESOURCE else None
_gc_before   = gc.get_count()
_wall_start  = time.monotonic()

# ---------------------------------------------------------------------------
# cProfile execution
# ---------------------------------------------------------------------------
_profiler    = cProfile.Profile()
_exception   = None

try:
    _profiler.enable()
    exec(compile(user_code, user_code_file, "exec"), {"__name__": "__main__"})
    _profiler.disable()
except Exception:
    _profiler.disable()
    import traceback as _tb
    _exception = _tb.format_exc()

# ---------------------------------------------------------------------------
# Resource — after
# ---------------------------------------------------------------------------
_wall_end  = time.monotonic()
_ru_after  = _resource.getrusage(_resource.RUSAGE_SELF) if _HAS_RESOURCE else None
_gc_after  = gc.get_count()

_stop_event.set()
_poll_thread.join(timeout=1.0)

# Final alloc snapshot
_final_snap  = tracemalloc.take_snapshot()
_final_stats = _final_snap.statistics("lineno")
tracemalloc.stop()
gc.callbacks.remove(_gc_cb)

# ---------------------------------------------------------------------------
# cProfile stats
# ---------------------------------------------------------------------------
_buf = io.StringIO()
_ps  = pstats.Stats(_profiler, stream=_buf)
_ps.sort_stats("cumulative")
_ps.print_stats(50)
_cprofile_text = _buf.getvalue()

_top_functions: list[dict] = []
for entry in sorted(_profiler.getstats(), key=lambda e: e.totaltime, reverse=True)[:50]:
    code = entry.code
    if hasattr(code, "co_filename"):
        fn, ln, nm = code.co_filename, code.co_firstlineno, code.co_name
    else:
        fn, ln, nm = str(code), 0, str(code)
    _top_functions.append({
        "filename": fn,
        "lineno":   ln,
        "name":     nm,
        "ncalls":   entry.callcount,
        "tottime":  entry.totaltime,
        "cumtime":  entry.totaltime,
    })

# ---------------------------------------------------------------------------
# Memory timeline
# ---------------------------------------------------------------------------
_t0 = _mem_snapshots[0]["t"] if _mem_snapshots else _wall_start
_mem_timeline = [{"t": s["t"] - _t0, "bytes": s["bytes"]} for s in _mem_snapshots]

# ---------------------------------------------------------------------------
# Resource usage
# ---------------------------------------------------------------------------
if _HAS_RESOURCE and _ru_before and _ru_after:
    _utime = _ru_after.ru_utime - _ru_before.ru_utime
    _stime = _ru_after.ru_stime - _ru_before.ru_stime
    _total = _utime + _stime
    _resource_usage = {
        "utime_s":    _utime,
        "stime_s":    _stime,
        "user_pct":   (_utime / _total * 100) if _total > 0 else 0.0,
        "kernel_pct": (_stime / _total * 100) if _total > 0 else 0.0,
        "maxrss_kb":  _ru_after.ru_maxrss,
        "ru_inblock": _ru_after.ru_inblock  - _ru_before.ru_inblock,
        "ru_oublock": _ru_after.ru_oublock  - _ru_before.ru_oublock,
        "ru_nvcsw":   _ru_after.ru_nvcsw   - _ru_before.ru_nvcsw,
        "ru_nivcsw":  _ru_after.ru_nivcsw  - _ru_before.ru_nivcsw,
    }
else:
    _resource_usage = {
        "utime_s": 0.0, "stime_s": 0.0,
        "user_pct": 0.0, "kernel_pct": 0.0,
        "maxrss_kb": 0, "ru_inblock": 0, "ru_oublock": 0,
        "ru_nvcsw": 0, "ru_nivcsw": 0,
    }

# ---------------------------------------------------------------------------
# GC stats
# ---------------------------------------------------------------------------
_gc_stats = {
    "count_before":      list(_gc_before),
    "count_after":       list(_gc_after),
    "events":            _gc_events,
    "total_collections": sum(_gc_after[i] - _gc_before[i] for i in range(3)),
}

# ---------------------------------------------------------------------------
# Tracemalloc allocation summary
# ---------------------------------------------------------------------------
_alloc_top: list[dict] = []
for stat in sorted(_final_stats, key=lambda s: s.size, reverse=True)[:30]:
    _alloc_top.append({
        "filename":   stat.traceback[0].filename if stat.traceback else "",
        "lineno":     stat.traceback[0].lineno   if stat.traceback else 0,
        "size_bytes": stat.size,
        "count":      stat.count,
    })

_total_bytes = sum(s.size  for s in _final_stats)
_total_count = sum(s.count for s in _final_stats)

# ---------------------------------------------------------------------------
# Write output
# ---------------------------------------------------------------------------
import os as _os
_output = {
    "cprofile": {
        "text":          _cprofile_text,
        "top_functions": _top_functions,
    },
    "memory_timeline":   _mem_timeline,
    "memory": {
        "peak_bytes":        max((s["bytes"] for s in _mem_snapshots), default=0),
        "total_alloc_bytes": _total_bytes,
        "total_alloc_count": _total_count,
        "top_allocations":   _alloc_top,
    },
    "resource_usage":    _resource_usage,
    "gc_stats":          _gc_stats,
    "runtime_ms":        (_wall_end - _wall_start) * 1000,
    "exception":         _exception,
}

with open(_os.path.join(job_dir, "profiling_output.json"), "w", encoding="utf-8") as _out:
    json.dump(_output, _out, indent=2, default=str)

if _exception:
    print(_exception, file=sys.stderr)
    sys.exit(1)
'''


# ---------------------------------------------------------------------------
# strace output parser
# ---------------------------------------------------------------------------

def _parse_strace(text: str) -> dict[str, Any]:
    """
    Parse the summary table produced by ``strace -c``.

    Returns a dict with keys:
      by_type: {syscall_name: {calls, errors, usecs_per_call, total_time_pct}}
      total_calls: int
      total_errors: int
    """
    by_type: dict[str, dict[str, Any]] = {}
    total_calls = 0
    total_errors = 0

    # strace -c output columns (varies by version):
    # % time  seconds  usecs/call  calls  errors  syscall
    # OR
    # % time  seconds  usecs/call  calls  errors  errors  syscall  (newer)
    _header_re = re.compile(r"^%\s+time\s+seconds", re.I)
    _row_re = re.compile(
        r"^\s*(?P<pct>[\d.]+)\s+"
        r"(?P<secs>[\d.]+)\s+"
        r"(?P<usecs>[\d.]+)\s+"
        r"(?P<calls>\d+)\s+"
        r"(?:(?P<errors>\d+)\s+)?"
        r"(?P<name>\w+)\s*$"
    )

    in_table = False
    for line in text.splitlines():
        if _header_re.match(line):
            in_table = True
            continue
        if not in_table:
            continue
        if line.startswith("---") or line.startswith("==="):
            continue
        m = _row_re.match(line)
        if m:
            name = m.group("name")
            calls = int(m.group("calls"))
            errors = int(m.group("errors") or 0)
            by_type[name] = {
                "calls": calls,
                "errors": errors,
                "usecs_per_call": float(m.group("usecs")),
                "time_pct": float(m.group("pct")),
            }
            total_calls += calls
            total_errors += errors

    return {
        "by_type": by_type,
        "total_calls": total_calls,
        "total_errors": total_errors,
    }


# ---------------------------------------------------------------------------
# PythonProfiler
# ---------------------------------------------------------------------------

class PythonProfiler:
    """
    Profile Python code using cProfile, tracemalloc, GC callbacks,
    resource.getrusage, and optionally strace.

    Usage::

        profiler = PythonProfiler()
        raw = await profiler.profile(code, job_id)
    """

    def __init__(
        self,
        jobs_dir: Path = JOBS_DIR,
        max_memory_mb: int = MAX_MEMORY_MB,
        timeout: int = DEFAULT_TIMEOUT,
        use_strace: bool = STRACE_AVAILABLE,
    ) -> None:
        self.jobs_dir = Path(jobs_dir)
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self.max_memory_mb = max_memory_mb
        self.timeout = timeout
        self.use_strace = use_strace and STRACE_AVAILABLE

    async def profile(self, code: str, job_id: str | None = None) -> dict[str, Any]:
        """
        Run the full profiling pipeline for *code*.

        Returns a raw profiling data dict with keys:
        ``cprofile``, ``memory_timeline``, ``memory``, ``resource_usage``,
        ``gc_stats``, ``strace``, ``runtime_ms``.
        """
        job_id = job_id or str(uuid.uuid4())
        job_dir = self.jobs_dir / job_id
        job_dir.mkdir(parents=True, exist_ok=True)

        try:
            return await self._run(code, job_id, job_dir)
        finally:
            shutil.rmtree(job_dir, ignore_errors=True)

    # ------------------------------------------------------------------
    # Internal pipeline
    # ------------------------------------------------------------------

    async def _run(
        self, code: str, job_id: str, job_dir: Path
    ) -> dict[str, Any]:
        # Write user code
        code_file = job_dir / "user_code.py"
        code_file.write_text(code, encoding="utf-8")

        # Write runner
        runner_file = job_dir / "profiler_runner.py"
        runner_file.write_text(_RUNNER_SCRIPT, encoding="utf-8")

        strace_out_file = job_dir / "strace_output.txt"

        if self.use_strace:
            cmd = [
                "strace", "-c", "-o", str(strace_out_file),
                sys.executable, str(runner_file), str(code_file), str(job_dir),
            ]
        else:
            cmd = [
                sys.executable, str(runner_file), str(code_file), str(job_dir),
            ]

        preexec_fn = None
        if IS_LINUX:
            import resource as _resource  # noqa: PLC0415

            def _preexec():
                mem = self.max_memory_mb * 1024 * 1024 * 2
                _resource.setrlimit(_resource.RLIMIT_AS, (mem, mem))
                _resource.setrlimit(
                    _resource.RLIMIT_CPU, (self.timeout, self.timeout)
                )

            preexec_fn = _preexec

        start = time.monotonic()
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                preexec_fn=preexec_fn,
                cwd=str(job_dir),
            )
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(), timeout=self.timeout + 10
            )
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception:
                pass
            return self._error("timeout", f"Profiling exceeded {self.timeout}s")

        elapsed_ms = (time.monotonic() - start) * 1000
        stderr_text = stderr_b.decode("utf-8", errors="replace")

        # Load primary output
        output_file = job_dir / "profiling_output.json"
        if not output_file.exists():
            return self._error(
                "runtime_error",
                "profiler_runner.py did not produce output",
                stderr_text,
            )

        try:
            raw: dict[str, Any] = json.loads(output_file.read_text("utf-8"))
        except json.JSONDecodeError as exc:
            return self._error("runtime_error", f"Bad profiler JSON: {exc}", stderr_text)

        # Merge strace data
        strace_data: dict[str, Any] | None = None
        if self.use_strace and strace_out_file.exists():
            try:
                strace_data = _parse_strace(
                    strace_out_file.read_text("utf-8", errors="replace")
                )
            except Exception as exc:
                logger.warning("strace parse failed: %s", exc)

        raw["strace"] = strace_data
        raw["_wall_ms"] = elapsed_ms

        return {"success": True, **raw}

    # ------------------------------------------------------------------

    @staticmethod
    def _error(
        error_type: str, message: str, stderr: str = ""
    ) -> dict[str, Any]:
        return {
            "success": False,
            "error_type": error_type,
            "message": message,
            "stderr": stderr,
            "cprofile": None,
            "memory_timeline": [],
            "memory": {},
            "resource_usage": {},
            "gc_stats": {},
            "strace": None,
            "runtime_ms": 0.0,
        }


# ---------------------------------------------------------------------------
# CLI helper (for quick standalone testing)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="DeepTrace Python Profiler")
    ap.add_argument("code_file", help="Python file to profile")
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    args = ap.parse_args()

    _code = Path(args.code_file).read_text()
    _profiler = PythonProfiler(timeout=args.timeout)
    _result = asyncio.run(_profiler.profile(_code))
    print(json.dumps(_result, indent=2, default=str))
