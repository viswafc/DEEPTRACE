"""
sandbox.py — DeepTrace SandboxExecutor
Safely executes user-submitted code with resource limits, security checks,
and automatic cleanup.  Supports Python and Java.
"""

from __future__ import annotations

import ast
import asyncio
import json
import logging
import os
import platform
import re
import shutil
import sys
import tempfile
import textwrap
import time
import uuid
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration constants
# ---------------------------------------------------------------------------
JOBS_DIR: Path = Path(os.environ.get("DEEPTRACE_JOBS_DIR", "/tmp/deeptrace_jobs"))
MAX_MEMORY_MB: int = int(os.environ.get("DEEPTRACE_MAX_MEMORY_MB", "512"))
DEFAULT_TIMEOUT: int = int(os.environ.get("DEEPTRACE_TIMEOUT_S", "30"))
JAVA_COMPILE_TIMEOUT: int = 30
IS_LINUX: bool = platform.system() == "Linux"

# Dangerous Python names that must not appear in user code.
_PYTHON_BLOCKED_IMPORTS: frozenset[str] = frozenset(
    {
        "os",
        "subprocess",
        "sys",
        "multiprocessing",
        "socket",
        "pty",
        "atexit",
        "signal",
        "ctypes",
        "cffi",
        "shutil",
        "pathlib",
        "importlib",
        "runpy",
        "pkgutil",
        "zipimport",
        "builtins",
    }
)

_PYTHON_BLOCKED_CALLS: frozenset[str] = frozenset(
    {
        "__import__",
        "eval",
        "exec",
        "compile",
        "open",
        "input",
        "breakpoint",
        "exit",
        "quit",
    }
)

# Java patterns that are unconditionally blocked.
_JAVA_BLOCKED_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\bRuntime\s*\.\s*(getRuntime\s*\(\s*\)\s*\.)?\s*exec\s*\(", re.S),
    re.compile(r"\bProcessBuilder\b", re.S),
    re.compile(r"\bSystem\s*\.\s*exit\s*\(", re.S),
    re.compile(r"\bFileWriter\b", re.S),
    re.compile(r"\bFileOutputStream\b", re.S),
    re.compile(r"\bReflection\b", re.S),
    re.compile(r"\bClass\.forName\s*\(", re.S),
    re.compile(r"\bClassLoader\b", re.S),
]


# ---------------------------------------------------------------------------
# Result helpers
# ---------------------------------------------------------------------------

def _ok(data: dict[str, Any]) -> dict[str, Any]:
    return {"success": True, **data}


def _err(error_type: str, message: str, stderr: str = "") -> dict[str, Any]:
    return {
        "success": False,
        "error_type": error_type,
        "message": message,
        "stderr": stderr,
    }


# ---------------------------------------------------------------------------
# Resource-limit preexec (Linux only)
# ---------------------------------------------------------------------------

def _make_preexec(memory_mb: int, cpu_seconds: int):
    """
    Return a callable suitable for subprocess preexec_fn that applies
    RLIMIT_AS and RLIMIT_CPU before exec.  Only valid on Linux.
    """
    def _preexec():
        import resource  # noqa: PLC0415 – only imported on Linux workers

        mem_bytes = memory_mb * 1024 * 1024 * 2  # virtual address space
        resource.setrlimit(resource.RLIMIT_AS, (mem_bytes, mem_bytes))
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
        # Prevent forking children
        resource.setrlimit(resource.RLIMIT_NPROC, (1, 1))

    return _preexec


# ---------------------------------------------------------------------------
# Security checks
# ---------------------------------------------------------------------------

class SecurityViolation(Exception):
    """Raised when static analysis finds unsafe constructs in user code."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _check_python(code: str) -> None:
    """
    Static AST-based security analysis for Python code.
    Raises SecurityViolation on any dangerous construct.
    """
    try:
        tree = ast.parse(code, mode="exec")
    except SyntaxError as exc:
        raise SecurityViolation(f"SyntaxError: {exc}") from exc

    for node in ast.walk(tree):
        # Blocked import statements
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = (
                [alias.name for alias in node.names]
                if isinstance(node, ast.Import)
                else ([node.module] if node.module else [])
            )
            for name in names:
                root = name.split(".")[0]
                if root in _PYTHON_BLOCKED_IMPORTS:
                    raise SecurityViolation(f"Blocked import: '{name}'")

        # Blocked function calls
        if isinstance(node, ast.Call):
            func = node.func
            # bare name call: open(...), eval(...), __import__(...)
            if isinstance(func, ast.Name) and func.id in _PYTHON_BLOCKED_CALLS:
                raise SecurityViolation(f"Blocked call: '{func.id}()'")
            # attribute call: os.system(...) — catch any access to blocked modules
            if isinstance(func, ast.Attribute):
                if isinstance(func.value, ast.Name):
                    if func.value.id in _PYTHON_BLOCKED_IMPORTS:
                        raise SecurityViolation(
                            f"Blocked attribute call: '{func.value.id}.{func.attr}()'"
                        )

        # Reject __dunder__ attribute access that could be dangerous
        if isinstance(node, ast.Attribute):
            if node.attr in (
                "__class__",
                "__bases__",
                "__subclasses__",
                "__globals__",
                "__builtins__",
                "__code__",
                "__loader__",
                "__spec__",
            ):
                raise SecurityViolation(
                    f"Blocked attribute access: '{node.attr}'"
                )


def _check_java(code: str) -> None:
    """
    Regex-based security analysis for Java code.
    Raises SecurityViolation on any dangerous construct.
    """
    for pattern in _JAVA_BLOCKED_PATTERNS:
        match = pattern.search(code)
        if match:
            raise SecurityViolation(
                f"Blocked Java construct near: '{match.group(0).strip()[:60]}'"
            )


# ---------------------------------------------------------------------------
# SandboxExecutor
# ---------------------------------------------------------------------------

class SandboxExecutor:
    """
    Safely execute user-submitted code with resource limits, security checks,
    and cleanup.  Returns a raw profiling output dict ready for the normalizer.
    """

    def __init__(
        self,
        jobs_dir: Path = JOBS_DIR,
        max_memory_mb: int = MAX_MEMORY_MB,
    ) -> None:
        self.jobs_dir = Path(jobs_dir)
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self.max_memory_mb = max_memory_mb

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def execute_python(
        self,
        code: str,
        job_id: str | None = None,
        timeout: int = DEFAULT_TIMEOUT,
    ) -> dict[str, Any]:
        """
        Execute Python code inside a sandboxed subprocess with the DeepTrace
        profiler runner.  Returns raw profiling output dict.
        """
        job_id = job_id or str(uuid.uuid4())
        job_dir = self.jobs_dir / job_id
        job_dir.mkdir(parents=True, exist_ok=True)

        try:
            # --- Security check ---
            try:
                _check_python(code)
            except SecurityViolation as exc:
                return _err("security_violation", str(exc))

            # --- Write user code ---
            code_file = job_dir / "user_code.py"
            code_file.write_text(code, encoding="utf-8")

            # --- Write profiler runner ---
            runner_file = job_dir / "profiler_runner.py"
            runner_file.write_text(_PROFILER_RUNNER_SCRIPT, encoding="utf-8")

            # --- Build command ---
            cmd = [
                sys.executable,
                str(runner_file),
                str(code_file),
                str(job_dir),
            ]

            preexec = _make_preexec(self.max_memory_mb, timeout) if IS_LINUX else None

            # --- Run ---
            start = time.monotonic()
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    preexec_fn=preexec,
                    cwd=str(job_dir),
                )
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout + 5
                )
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except Exception:
                    pass
                return _err(
                    "timeout",
                    f"Execution exceeded {timeout}s wall-clock timeout",
                )

            elapsed_ms = (time.monotonic() - start) * 1000
            stdout = stdout_bytes.decode("utf-8", errors="replace")
            stderr = stderr_bytes.decode("utf-8", errors="replace")

            if proc.returncode != 0:
                # Distinguish OOM from generic runtime errors
                if "MemoryError" in stderr or "Cannot allocate memory" in stderr:
                    return _err("memory_limit", "Process exceeded memory limit", stderr)
                return _err("runtime_error", "Python process exited with non-zero code", stderr)

            # --- Read profiling output ---
            output_file = job_dir / "profiling_output.json"
            if output_file.exists():
                try:
                    raw = json.loads(output_file.read_text(encoding="utf-8"))
                    raw["_wall_ms"] = elapsed_ms
                    raw["_stdout"] = stdout
                    raw["_stderr"] = stderr
                    return _ok(raw)
                except json.JSONDecodeError as exc:
                    logger.warning("Failed to parse profiling_output.json: %s", exc)
                    return _err("runtime_error", f"Invalid profiler output: {exc}", stderr)
            else:
                return _err(
                    "runtime_error",
                    "Profiler runner did not produce output file",
                    stderr,
                )

        finally:
            self._cleanup(job_dir)

    async def execute_java(
        self,
        code: str,
        job_id: str | None = None,
        timeout: int = DEFAULT_TIMEOUT,
    ) -> dict[str, Any]:
        """
        Compile and execute Java code under JFR.  Returns raw profiling output
        dict with gc_timeline, memory_timeline, and runtime_ms keys.
        """
        import shutil as _shutil

        job_id = job_id or str(uuid.uuid4())
        job_dir = self.jobs_dir / job_id
        job_dir.mkdir(parents=True, exist_ok=True)

        try:
            # --- Security check ---
            try:
                _check_java(code)
            except SecurityViolation as exc:
                return _err("security_violation", str(exc))

            # --- Wrap code in class if needed ---
            java_source = _wrap_java(code)

            # --- Write source ---
            src_file = job_dir / "Submission.java"
            src_file.write_text(java_source, encoding="utf-8")

            # --- Check javac availability ---
            javac = _shutil.which("javac")
            java = _shutil.which("java")
            if not javac or not java:
                logger.warning("Java toolchain not found; returning synthetic metrics")
                return _ok(_synthetic_java_metrics(job_id))

            # --- Compile ---
            compile_start = time.monotonic()
            try:
                compile_proc = await asyncio.create_subprocess_exec(
                    javac, str(src_file),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=str(job_dir),
                )
                c_out, c_err = await asyncio.wait_for(
                    compile_proc.communicate(), timeout=JAVA_COMPILE_TIMEOUT
                )
            except asyncio.TimeoutError:
                return _err("compile_error", "javac timed out after 30s")

            if compile_proc.returncode != 0:
                return _err(
                    "compile_error",
                    "javac returned non-zero exit code",
                    c_err.decode("utf-8", errors="replace"),
                )

            # --- Run with JFR ---
            jfr_file = job_dir / "recording.jfr"
            run_cmd = [
                java,
                "-XX:+UnlockDiagnosticVMOptions",
                f"-XX:StartFlightRecording=duration={timeout}s,filename={jfr_file}",
                f"-Xmx{self.max_memory_mb}m",
                "-Xss512k",
                "-cp", str(job_dir),
                "Submission",
            ]

            run_start = time.monotonic()
            try:
                run_proc = await asyncio.create_subprocess_exec(
                    *run_cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=str(job_dir),
                )
                # Poll jstat while process runs
                memory_timeline = await _poll_memory_and_wait(
                    run_proc, job_dir, timeout
                )
            except asyncio.TimeoutError:
                try:
                    run_proc.kill()
                except Exception:
                    pass
                return _err("timeout", f"Java execution exceeded {timeout}s")

            r_out, r_err = await run_proc.communicate()
            runtime_ms = (time.monotonic() - run_start) * 1000

            if run_proc.returncode not in (0, 143):  # 143 = SIGTERM
                return _err(
                    "runtime_error",
                    "Java process exited with non-zero code",
                    r_err.decode("utf-8", errors="replace"),
                )

            # --- Collect jstat GC data ---
            gc_timeline = _read_jstat_log(job_dir)

            return _ok(
                {
                    "gc_timeline": gc_timeline,
                    "memory_timeline": memory_timeline,
                    "runtime_ms": runtime_ms,
                    "compile_time_ms": (time.monotonic() - compile_start) * 1000
                    - runtime_ms,
                    "stdout": r_out.decode("utf-8", errors="replace"),
                    "stderr": r_err.decode("utf-8", errors="replace"),
                    "strace": None,
                    "compile_error": None,
                }
            )

        finally:
            self._cleanup(job_dir)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _cleanup(self, job_dir: Path) -> None:
        try:
            shutil.rmtree(job_dir, ignore_errors=True)
        except Exception as exc:
            logger.warning("Cleanup failed for %s: %s", job_dir, exc)


# ---------------------------------------------------------------------------
# Java helpers
# ---------------------------------------------------------------------------

def _wrap_java(code: str) -> str:
    """
    If the user code does not already declare a public class Submission,
    wrap it in one with a main method.
    """
    if re.search(r"\bclass\s+Submission\b", code):
        return code
    # If there is any class declaration, rename it Submission
    if re.search(r"\bpublic\s+class\s+\w+", code):
        code = re.sub(r"public\s+class\s+\w+", "public class Submission", code, count=1)
        return code
    # Plain statements — wrap in class + main
    return textwrap.dedent(
        f"""\
        public class Submission {{
            public static void main(String[] args) throws Exception {{
                {textwrap.indent(code, '        ').strip()}
            }}
        }}
        """
    )


async def _poll_memory_and_wait(
    proc: asyncio.subprocess.Process,
    job_dir: Path,
    timeout: int,
) -> list[dict[str, Any]]:
    """
    Poll process RSS every 200 ms while the Java process is running.
    Returns list of {t, rss_mb} dicts.
    """
    import psutil  # optional dependency

    timeline: list[dict[str, Any]] = []
    start = time.monotonic()
    deadline = start + timeout

    try:
        ps = psutil.Process(proc.pid)
    except Exception:
        # psutil not available or process already gone
        await asyncio.wait_for(proc.wait(), timeout=timeout)
        return timeline

    while True:
        now = time.monotonic()
        if now >= deadline:
            break
        try:
            mem_info = ps.memory_info()
            timeline.append(
                {"t": now - start, "rss_mb": mem_info.rss / (1024 * 1024)}
            )
        except Exception:
            break

        # Check if process has ended
        try:
            ret = proc.returncode
            if ret is not None:
                break
        except Exception:
            break

        await asyncio.sleep(0.2)

    return timeline


def _read_jstat_log(job_dir: Path) -> list[dict[str, Any]]:
    """Read jstat output file written by a background poller, if present."""
    jstat_log = job_dir / "jstat.log"
    if not jstat_log.exists():
        return []

    timeline: list[dict[str, Any]] = []
    lines = jstat_log.read_text(encoding="utf-8").splitlines()
    header: list[str] = []
    t = 0.0
    for line in lines:
        parts = line.split()
        if not parts:
            continue
        if parts[0] == "S0C":  # header line
            header = [p.lower() for p in parts]
            continue
        if header and len(parts) == len(header):
            try:
                row = dict(zip(header, (float(p) for p in parts)))
                row["t"] = t
                timeline.append(row)
                t += 0.1
            except ValueError:
                pass
    return timeline


def _synthetic_java_metrics(job_id: str) -> dict[str, Any]:
    """Return placeholder metrics when Java toolchain is unavailable."""
    return {
        "gc_timeline": [],
        "memory_timeline": [],
        "runtime_ms": 0.0,
        "compile_time_ms": 0.0,
        "stdout": "",
        "stderr": "",
        "strace": None,
        "compile_error": None,
        "note": "Java toolchain not available; metrics are empty placeholders.",
        "_synthetic": True,
    }


# ---------------------------------------------------------------------------
# Profiler runner script (embedded; written to temp dir at runtime)
# ---------------------------------------------------------------------------

_PROFILER_RUNNER_SCRIPT: str = r'''#!/usr/bin/env python3
"""
profiler_runner.py — DeepTrace Python Profiler Runner
Self-contained script that profiles a user code file and writes results to
profiling_output.json in the job directory.

Usage:
    python profiler_runner.py <user_code_file> <job_dir>
"""

import cProfile
import gc
import io
import json
import pstats
import resource
import sys
import threading
import time
import tracemalloc

# ---------------------------------------------------------------------------
# Args
# ---------------------------------------------------------------------------
if len(sys.argv) < 3:
    print("Usage: profiler_runner.py <user_code_file> <job_dir>", file=sys.stderr)
    sys.exit(1)

user_code_file = sys.argv[1]
job_dir = sys.argv[2]

with open(user_code_file, "r", encoding="utf-8") as fh:
    user_code = fh.read()

# ---------------------------------------------------------------------------
# Tracemalloc
# ---------------------------------------------------------------------------
tracemalloc.start(25)

# ---------------------------------------------------------------------------
# Memory snapshot thread
# ---------------------------------------------------------------------------
_mem_snapshots: list[dict] = []
_stop_mem_thread = threading.Event()


def _mem_thread():
    while not _stop_mem_thread.is_set():
        snapshot = tracemalloc.take_snapshot()
        stats = snapshot.statistics("lineno")
        total_bytes = sum(s.size for s in stats)
        _mem_snapshots.append({"t": time.monotonic(), "bytes": total_bytes})
        _stop_mem_thread.wait(timeout=0.1)


_t = threading.Thread(target=_mem_thread, daemon=True)
_t.start()

# ---------------------------------------------------------------------------
# GC callbacks
# ---------------------------------------------------------------------------
_gc_events: list[dict] = []
_gc_phase_start: float | None = None


def _gc_callback(phase: str, info: dict):
    global _gc_phase_start
    if phase == "start":
        _gc_phase_start = time.monotonic()
    elif phase == "stop" and _gc_phase_start is not None:
        pause_ms = (time.monotonic() - _gc_phase_start) * 1000
        gen = info.get("generation", 0)
        gc_type = "major" if gen == 2 else "minor"
        _gc_events.append(
            {
                "t": _gc_phase_start,
                "pause_ms": pause_ms,
                "type": gc_type,
                "generation": gen,
                "collected": info.get("collected", 0),
                "uncollectable": info.get("uncollectable", 0),
            }
        )
        _gc_phase_start = None


gc.callbacks.append(_gc_callback)

# ---------------------------------------------------------------------------
# Resource usage — before
# ---------------------------------------------------------------------------
_rusage_before = resource.getrusage(resource.RUSAGE_SELF)
_gc_count_before = gc.get_count()
_wall_start = time.monotonic()

# ---------------------------------------------------------------------------
# cProfile execution
# ---------------------------------------------------------------------------
_profiler = cProfile.Profile()
_exec_exception: str | None = None

try:
    _profiler.enable()
    exec(compile(user_code, user_code_file, "exec"), {"__name__": "__main__"})
    _profiler.disable()
except Exception as exc:
    _profiler.disable()
    import traceback
    _exec_exception = traceback.format_exc()

# ---------------------------------------------------------------------------
# Resource usage — after
# ---------------------------------------------------------------------------
_wall_end = time.monotonic()
_rusage_after = resource.getrusage(resource.RUSAGE_SELF)
_gc_count_after = gc.get_count()

# Stop memory snapshot thread
_stop_mem_thread.set()
_t.join(timeout=1.0)

# Final memory snapshot
_final_snapshot = tracemalloc.take_snapshot()
_final_stats = _final_snapshot.statistics("lineno")
tracemalloc.stop()

# Remove our GC callback
gc.callbacks.remove(_gc_callback)

# ---------------------------------------------------------------------------
# cProfile stats extraction
# ---------------------------------------------------------------------------
_stats_stream = io.StringIO()
_ps = pstats.Stats(_profiler, stream=_stats_stream)
_ps.sort_stats("cumulative")
_ps.print_stats(50)
_cprofile_text = _stats_stream.getvalue()

# Build top-functions list
_top_functions: list[dict] = []
for key, (cc, nc, tt, ct, callers) in _profiler.getstats().__class__.__mro__[0].__dict__.items() if False else []:
    pass

_profiler_stats = _profiler.getstats()
for entry in sorted(_profiler_stats, key=lambda e: e.totaltime, reverse=True)[:50]:
    code = entry.code
    if hasattr(code, "co_filename"):
        fname = code.co_filename
        lineno = code.co_firstlineno
        name = code.co_name
    else:
        fname = str(code)
        lineno = 0
        name = str(code)
    _top_functions.append(
        {
            "filename": fname,
            "lineno": lineno,
            "name": name,
            "ncalls": entry.callcount,
            "tottime": entry.totaltime,
            "cumtime": entry.totaltime,  # approximate
        }
    )

# ---------------------------------------------------------------------------
# Memory timeline normalisation
# ---------------------------------------------------------------------------
_t0 = _mem_snapshots[0]["t"] if _mem_snapshots else _wall_start
_mem_timeline = [
    {"t": s["t"] - _t0, "bytes": s["bytes"]} for s in _mem_snapshots
]

# ---------------------------------------------------------------------------
# Resource delta
# ---------------------------------------------------------------------------
_utime = _rusage_after.ru_utime - _rusage_before.ru_utime
_stime = _rusage_after.ru_stime - _rusage_before.ru_stime
_total_cpu = _utime + _stime

_resource_usage = {
    "utime_s": _utime,
    "stime_s": _stime,
    "user_pct": (_utime / _total_cpu * 100) if _total_cpu > 0 else 0.0,
    "kernel_pct": (_stime / _total_cpu * 100) if _total_cpu > 0 else 0.0,
    "maxrss_kb": _rusage_after.ru_maxrss,
    "ru_inblock": _rusage_after.ru_inblock - _rusage_before.ru_inblock,
    "ru_oublock": _rusage_after.ru_oublock - _rusage_before.ru_oublock,
    "ru_nvcsw": _rusage_after.ru_nvcsw - _rusage_before.ru_nvcsw,
    "ru_nivcsw": _rusage_after.ru_nivcsw - _rusage_before.ru_nivcsw,
}

# ---------------------------------------------------------------------------
# GC stats
# ---------------------------------------------------------------------------
_gc_stats = {
    "count_before": list(_gc_count_before),
    "count_after": list(_gc_count_after),
    "events": _gc_events,
    "total_collections": sum(
        _gc_count_after[i] - _gc_count_before[i] for i in range(3)
    ),
}

# ---------------------------------------------------------------------------
# Tracemalloc top allocations
# ---------------------------------------------------------------------------
_alloc_top: list[dict] = []
for stat in sorted(_final_stats, key=lambda s: s.size, reverse=True)[:30]:
    _alloc_top.append(
        {
            "filename": stat.traceback[0].filename if stat.traceback else "",
            "lineno": stat.traceback[0].lineno if stat.traceback else 0,
            "size_bytes": stat.size,
            "count": stat.count,
        }
    )

_total_alloc_bytes = sum(s.size for s in _final_stats)
_total_alloc_count = sum(s.count for s in _final_stats)

# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------
_output = {
    "cprofile": {
        "text": _cprofile_text,
        "top_functions": _top_functions,
    },
    "memory_timeline": _mem_timeline,
    "memory": {
        "peak_bytes": max((s["bytes"] for s in _mem_snapshots), default=0),
        "total_alloc_bytes": _total_alloc_bytes,
        "total_alloc_count": _total_alloc_count,
        "top_allocations": _alloc_top,
    },
    "resource_usage": _resource_usage,
    "gc_stats": _gc_stats,
    "runtime_ms": (_wall_end - _wall_start) * 1000,
    "exception": _exec_exception,
}

import os
_out_path = os.path.join(job_dir, "profiling_output.json")
with open(_out_path, "w", encoding="utf-8") as _fh:
    json.dump(_output, _fh, indent=2, default=str)

if _exec_exception:
    print(_exec_exception, file=sys.stderr)
    sys.exit(1)
'''
