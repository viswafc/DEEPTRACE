"""
profiler_java.py — DeepTrace JavaProfiler
Profile Java code using JFR, jstat GC polling, psutil memory polling,
and optional strace wrapping.
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
import textwrap
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
JAVA_COMPILE_TIMEOUT: int = 30

# ---------------------------------------------------------------------------
# Source wrapping
# ---------------------------------------------------------------------------

def wrap_java_source(code: str) -> str:
    """
    Ensure the code is a complete, compilable ``Submission`` class with a
    ``main`` method.  Three cases are handled:

    1. Already declares ``class Submission`` — return as-is.
    2. Declares some other ``public class`` — rename it ``Submission``.
    3. Raw statements — wrap in ``public class Submission { main { ... } }``.
    """
    if re.search(r"\bclass\s+Submission\b", code):
        return code
    if re.search(r"\bpublic\s+class\s+\w+", code):
        return re.sub(
            r"public\s+class\s+(\w+)",
            "public class Submission",
            code,
            count=1,
        )
    indented = textwrap.indent(code.strip(), "        ")
    return textwrap.dedent(
        f"""\
        public class Submission {{
            public static void main(String[] args) throws Exception {{
        {indented}
            }}
        }}
        """
    )


# ---------------------------------------------------------------------------
# strace parser (reused from profiler_python)
# ---------------------------------------------------------------------------

def _parse_strace(text: str) -> dict[str, Any]:
    by_type: dict[str, dict[str, Any]] = {}
    total_calls = 0
    total_errors = 0

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
# jstat parser
# ---------------------------------------------------------------------------

def parse_jstat_output(text: str) -> list[dict[str, Any]]:
    """
    Parse ``jstat -gc <pid> 100`` columnar output into a list of row dicts.

    jstat columns (from -gc):
        S0C  S1C  S0U  S1U  EC  EU  OC  OU  MC  MU
        CCSC CCSU YGC  YGCT FGC FGCT CGC CGCT GCT
    """
    rows: list[dict[str, Any]] = []
    lines = [l for l in text.splitlines() if l.strip()]
    if not lines:
        return rows

    header: list[str] = []
    t = 0.0
    for line in lines:
        parts = line.split()
        if not parts:
            continue
        # Detect header (contains non-numeric tokens)
        if parts[0].upper() == parts[0] and not parts[0][0].isdigit():
            header = [p.lower() for p in parts]
            continue
        if header and len(parts) >= len(header):
            try:
                values = [float(p) for p in parts[: len(header)]]
                row: dict[str, Any] = dict(zip(header, values))
                row["t"] = t
                rows.append(row)
                t += 0.1
            except ValueError:
                pass
    return rows


def detect_gc_events_from_jstat(
    timeline: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Derive GC events from increments in the YGC / FGC counters across
    consecutive jstat rows.
    """
    events: list[dict[str, Any]] = []
    prev: dict[str, Any] | None = None
    for row in timeline:
        if prev is None:
            prev = row
            continue

        ygc_delta = row.get("ygc", 0) - prev.get("ygc", 0)
        fgc_delta = row.get("fgc", 0) - prev.get("fgc", 0)

        for _ in range(int(ygc_delta)):
            events.append(
                {
                    "t": row["t"],
                    "type": "minor",
                    "pause_ms": (row.get("ygct", 0) - prev.get("ygct", 0))
                    * 1000
                    / max(ygc_delta, 1),
                }
            )
        for _ in range(int(fgc_delta)):
            events.append(
                {
                    "t": row["t"],
                    "type": "major",
                    "pause_ms": (row.get("fgct", 0) - prev.get("fgct", 0))
                    * 1000
                    / max(fgc_delta, 1),
                }
            )
        prev = row

    return events


# ---------------------------------------------------------------------------
# Memory poller (psutil)
# ---------------------------------------------------------------------------

async def _poll_process_memory(
    pid: int,
    interval_s: float,
    stop_event: asyncio.Event,
    wall_start: float,
) -> list[dict[str, Any]]:
    """
    Poll RSS of *pid* every *interval_s* seconds until *stop_event* is set.
    Returns list of {t, rss_mb}.
    """
    timeline: list[dict[str, Any]] = []
    try:
        import psutil  # optional

        ps = psutil.Process(pid)
        while not stop_event.is_set():
            try:
                mem = ps.memory_info()
                timeline.append(
                    {
                        "t": time.monotonic() - wall_start,
                        "rss_mb": mem.rss / (1024 * 1024),
                    }
                )
            except psutil.NoSuchProcess:
                break
            try:
                await asyncio.wait_for(
                    asyncio.shield(stop_event.wait()), timeout=interval_s
                )
            except asyncio.TimeoutError:
                pass
    except ImportError:
        logger.debug("psutil not available — memory timeline will be empty")
    return timeline


# ---------------------------------------------------------------------------
# jstat background poller
# ---------------------------------------------------------------------------

async def _run_jstat_poller(
    pid: int,
    job_dir: Path,
    interval_ms: int = 100,
) -> asyncio.subprocess.Process | None:
    """Start a jstat process that writes to job_dir/jstat.log."""
    jstat = shutil.which("jstat")
    if not jstat:
        return None
    log_file = job_dir / "jstat.log"
    try:
        proc = await asyncio.create_subprocess_exec(
            jstat, "-gc", str(pid), str(interval_ms),
            stdout=open(log_file, "w"),  # noqa: WPS515
            stderr=asyncio.subprocess.DEVNULL,
        )
        return proc
    except Exception as exc:
        logger.debug("jstat start failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# JavaProfiler
# ---------------------------------------------------------------------------

class JavaProfiler:
    """
    Profile Java code using JFR, jstat GC polling, psutil memory polling,
    and optionally strace.

    Usage::

        profiler = JavaProfiler()
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
        Full profiling pipeline.

        Returns a dict with keys:
        ``gc_timeline``, ``gc_events``, ``memory_timeline``, ``runtime_ms``,
        ``compile_time_ms``, ``strace``, ``compile_error``, ``stdout``,
        ``stderr``.
        """
        job_id = job_id or str(uuid.uuid4())
        job_dir = self.jobs_dir / job_id
        job_dir.mkdir(parents=True, exist_ok=True)

        try:
            return await self._run(code, job_dir)
        finally:
            shutil.rmtree(job_dir, ignore_errors=True)

    # ------------------------------------------------------------------
    # Internal pipeline
    # ------------------------------------------------------------------

    async def _run(self, code: str, job_dir: Path) -> dict[str, Any]:
        javac = shutil.which("javac")
        java = shutil.which("java")

        if not javac or not java:
            logger.warning("Java toolchain not available — returning synthetic metrics")
            return self._synthetic()

        # --- Write source ---
        java_source = wrap_java_source(code)
        src_file = job_dir / "Submission.java"
        src_file.write_text(java_source, encoding="utf-8")

        # --- Compile ---
        compile_start = time.monotonic()
        try:
            cp = await asyncio.create_subprocess_exec(
                javac, str(src_file),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(job_dir),
            )
            c_out, c_err = await asyncio.wait_for(
                cp.communicate(), timeout=JAVA_COMPILE_TIMEOUT
            )
        except asyncio.TimeoutError:
            return self._error("compile_error", f"javac timed out after {JAVA_COMPILE_TIMEOUT}s")

        compile_ms = (time.monotonic() - compile_start) * 1000

        if cp.returncode != 0:
            return self._error(
                "compile_error",
                "Compilation failed",
                c_err.decode("utf-8", errors="replace"),
            )

        # --- Build run command ---
        jfr_file = job_dir / "recording.jfr"
        run_cmd = [
            java,
            "-XX:+UnlockDiagnosticVMOptions",
            f"-XX:StartFlightRecording=duration={self.timeout}s,filename={jfr_file}",
            f"-Xmx{self.max_memory_mb}m",
            "-Xss512k",
            "-cp", str(job_dir),
            "Submission",
        ]

        if self.use_strace:
            strace_out = job_dir / "strace_java.txt"
            run_cmd = [
                "strace", "-c", "-o", str(strace_out),
                *run_cmd,
            ]

        # --- Launch process ---
        wall_start = time.monotonic()
        try:
            rp = await asyncio.create_subprocess_exec(
                *run_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(job_dir),
            )
        except Exception as exc:
            return self._error("runtime_error", f"Failed to start JVM: {exc}")

        # --- Side pollers ---
        stop_mem = asyncio.Event()
        mem_task = asyncio.create_task(
            _poll_process_memory(rp.pid, 0.2, stop_mem, wall_start)
        )
        jstat_proc = await _run_jstat_poller(rp.pid, job_dir, interval_ms=100)

        # --- Wait ---
        try:
            r_out, r_err = await asyncio.wait_for(
                rp.communicate(), timeout=self.timeout + 10
            )
        except asyncio.TimeoutError:
            try:
                rp.kill()
            except Exception:
                pass
            stop_mem.set()
            mem_timeline = await mem_task
            if jstat_proc:
                jstat_proc.terminate()
            return self._error("timeout", f"Java execution exceeded {self.timeout}s")

        runtime_ms = (time.monotonic() - wall_start) * 1000

        # Stop pollers
        stop_mem.set()
        memory_timeline = await mem_task
        if jstat_proc:
            try:
                jstat_proc.terminate()
                await asyncio.wait_for(jstat_proc.wait(), timeout=2)
            except Exception:
                pass

        # --- Parse jstat ---
        jstat_log = job_dir / "jstat.log"
        gc_timeline: list[dict[str, Any]] = []
        gc_events: list[dict[str, Any]] = []
        if jstat_log.exists():
            gc_timeline = parse_jstat_output(
                jstat_log.read_text("utf-8", errors="replace")
            )
            gc_events = detect_gc_events_from_jstat(gc_timeline)

        # --- Parse strace ---
        strace_data: dict[str, Any] | None = None
        if self.use_strace:
            strace_file = job_dir / "strace_java.txt"
            if strace_file.exists():
                try:
                    strace_data = _parse_strace(
                        strace_file.read_text("utf-8", errors="replace")
                    )
                except Exception as exc:
                    logger.warning("strace parse failed: %s", exc)

        if rp.returncode not in (0, 143):
            logger.warning(
                "JVM exited with code %d; stderr: %s",
                rp.returncode,
                r_err.decode("utf-8", errors="replace")[:200],
            )

        return {
            "success": True,
            "gc_timeline": gc_timeline,
            "gc_events": gc_events,
            "memory_timeline": memory_timeline,
            "runtime_ms": runtime_ms,
            "compile_time_ms": compile_ms,
            "stdout": r_out.decode("utf-8", errors="replace"),
            "stderr": r_err.decode("utf-8", errors="replace"),
            "strace": strace_data,
            "compile_error": None,
        }

    # ------------------------------------------------------------------
    # Helpers
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
            "gc_timeline": [],
            "gc_events": [],
            "memory_timeline": [],
            "runtime_ms": 0.0,
            "compile_time_ms": 0.0,
            "stdout": "",
            "strace": None,
            "compile_error": stderr if error_type == "compile_error" else None,
        }

    @staticmethod
    def _synthetic() -> dict[str, Any]:
        return {
            "success": True,
            "gc_timeline": [],
            "gc_events": [],
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
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="DeepTrace Java Profiler")
    ap.add_argument("code_file", help="Java source file to profile")
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    args = ap.parse_args()

    _code = Path(args.code_file).read_text()
    _jp = JavaProfiler(timeout=args.timeout)
    _result = asyncio.run(_jp.profile(_code))
    print(json.dumps(_result, indent=2, default=str))
