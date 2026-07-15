"""
routers/submission.py – Code submission, status, and health endpoints.

Endpoints
---------
POST /api/submit
    Accept source code + language, validate, sanitise, enqueue profiling job.
GET  /api/status/{job_id}
    Lightweight polling endpoint for job progress.
GET  /api/health
    Liveness probe with environment capability flags.
"""

from __future__ import annotations

import ast
import logging
import re
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, HTTPException, status
from pydantic import BaseModel, Field, field_validator

from config import settings
from models.job import JobStatus
from profiler.engine import run_profiling_job
from storage.store import job_store

logger = logging.getLogger(__name__)

router = APIRouter(tags=["submission"])

# ─────────────────────────────────────────────────────────────────────────────
# Constants & security patterns
# ─────────────────────────────────────────────────────────────────────────────

_MAX_CODE_BYTES = 50 * 1024  # 50 KB

# Patterns that are unconditionally forbidden in submitted Python code.
_PYTHON_BLOCKED_PATTERNS: list[re.Pattern] = [
    re.compile(r"\bimport\s+os\b"),
    re.compile(r"\bimport\s+subprocess\b"),
    re.compile(r"\bimport\s+sys\b"),
    re.compile(r"\bimport\s+shutil\b"),
    re.compile(r"\bimport\s+socket\b"),
    re.compile(r"\bimport\s+ctypes\b"),
    re.compile(r"\bimport\s+multiprocessing\b"),
    re.compile(r"\bimport\s+threading\b"),
    re.compile(r"\bfrom\s+os\b"),
    re.compile(r"\bfrom\s+subprocess\b"),
    re.compile(r"\bfrom\s+sys\b"),
    re.compile(r"\b__import__\s*\("),
    re.compile(r"\beval\s*\("),
    re.compile(r"\bexec\s*\("),
    re.compile(r"\bopen\s*\("),
    re.compile(r"\bcompile\s*\("),
    re.compile(r"\bgetattr\s*\("),
    re.compile(r"\bsetattr\s*\("),
    re.compile(r"\bdelattr\s*\("),
    re.compile(r"__builtins__"),
    re.compile(r"__globals__"),
    re.compile(r"__class__"),
]

# Patterns forbidden in Java submissions.
_JAVA_BLOCKED_PATTERNS: list[re.Pattern] = [
    re.compile(r"\bRuntime\.getRuntime\(\)"),
    re.compile(r"\bProcessBuilder\b"),
    re.compile(r"\bSystem\.exit\s*\("),
    re.compile(r"\bjava\.lang\.reflect\b"),
    re.compile(r"\bClassLoader\b"),
    re.compile(r"\bnew\s+Thread\s*\("),
    re.compile(r"\bjava\.net\."),
    re.compile(r"\bjava\.io\.File\b"),
    re.compile(r"\bjava\.nio\b"),
]


def _check_python_ast(code: str) -> None:
    """
    Parse the submitted Python code with the AST module and reject dangerous
    node types that cannot be caught by simple regex.

    Raises
    ------
    ValueError: If the code contains a forbidden construct.
    """
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        raise ValueError(f"Python syntax error: {exc}") from exc

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in {
                    "os", "subprocess", "sys", "shutil", "socket",
                    "ctypes", "multiprocessing", "threading", "importlib",
                    "pickle", "shelve", "signal", "pty",
                }:
                    raise ValueError(
                        f"Import of '{alias.name}' is not permitted."
                    )
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.module.split(".")[0] in {
                "os", "subprocess", "sys", "shutil", "socket",
                "ctypes", "multiprocessing", "threading", "importlib",
                "pickle", "shelve", "signal", "pty",
            }:
                raise ValueError(
                    f"Import from '{node.module}' is not permitted."
                )
        elif isinstance(node, ast.Call):
            # Block calls like eval(), exec(), open(), __import__()
            if isinstance(node.func, ast.Name) and node.func.id in {
                "eval", "exec", "open", "compile", "__import__",
                "breakpoint", "input",
            }:
                raise ValueError(
                    f"Call to built-in '{node.func.id}()' is not permitted."
                )


def _validate_code_security(language: str, code: str) -> None:
    """
    Run security checks appropriate to the given language.

    Parameters
    ----------
    language: 'python' or 'java'.
    code:     Raw source code string.

    Raises
    ------
    ValueError: If a security violation is detected.
    """
    if language == "python":
        for pattern in _PYTHON_BLOCKED_PATTERNS:
            if pattern.search(code):
                raise ValueError(
                    f"Code contains a forbidden pattern: {pattern.pattern}"
                )
        _check_python_ast(code)

    elif language == "java":
        for pattern in _JAVA_BLOCKED_PATTERNS:
            if pattern.search(code):
                raise ValueError(
                    f"Java code contains a forbidden pattern: {pattern.pattern}"
                )


# ─────────────────────────────────────────────────────────────────────────────
# Request / response schemas
# ─────────────────────────────────────────────────────────────────────────────


class SubmitRequest(BaseModel):
    """Body schema for POST /api/submit."""

    language: str = Field(..., description="Target language: 'python' or 'java'.")
    code: str = Field(..., description="Source code to profile.")

    @field_validator("language")
    @classmethod
    def validate_language(cls, v: str) -> str:
        """Ensure the submitted language is in the configured allow-list."""
        normalised = v.strip().lower()
        if normalised not in settings.ALLOWED_LANGUAGES:
            raise ValueError(
                f"Language '{v}' is not supported. "
                f"Allowed: {settings.ALLOWED_LANGUAGES}"
            )
        return normalised

    @field_validator("code")
    @classmethod
    def validate_code_not_empty(cls, v: str) -> str:
        """Reject empty or whitespace-only code submissions."""
        if not v or not v.strip():
            raise ValueError("Code must not be empty.")
        if len(v.encode("utf-8")) > _MAX_CODE_BYTES:
            raise ValueError(
                f"Code exceeds the maximum allowed size of {_MAX_CODE_BYTES // 1024} KB."
            )
        return v


class SubmitResponse(BaseModel):
    """Response schema for POST /api/submit."""

    job_id: str
    status: str


class StatusResponse(BaseModel):
    """Response schema for GET /api/status/{job_id}."""

    job_id: str
    status: str
    progress: int
    runtime_ms: float | None
    error: str | None


class HealthResponse(BaseModel):
    """Response schema for GET /api/health."""

    status: str
    version: str
    java_available: bool
    strace_available: bool


# ─────────────────────────────────────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────────────────────────────────────


@router.post(
    "/submit",
    response_model=SubmitResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Submit code for profiling",
)
async def submit_code(
    body: SubmitRequest,
    background_tasks: BackgroundTasks,
) -> SubmitResponse:
    """
    Validate, sanitise, and enqueue a profiling job.

    The job is created immediately in QUEUED state and its ID is returned.
    Profiling is performed asynchronously; poll /api/status/{job_id} or
    connect to /ws/{job_id} for live progress.

    Raises
    ------
    422: If the request body fails validation.
    400: If the code contains security violations.
    """
    # Security check
    try:
        _validate_code_security(body.language, body.code)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    job = await job_store.create_job(language=body.language, code=body.code)
    background_tasks.add_task(run_profiling_job, job.id)
    logger.info("Enqueued job %s (%s, %d bytes)", job.id, body.language, len(body.code))

    return SubmitResponse(job_id=job.id, status=JobStatus.QUEUED)


@router.get(
    "/status/{job_id}",
    response_model=StatusResponse,
    summary="Poll job status",
)
async def get_status(job_id: str) -> StatusResponse:
    """
    Return the current lifecycle state and progress of a profiling job.

    Raises
    ------
    404: If no job with the given ID exists.
    """
    job = await job_store.get_job(job_id)
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Job '{job_id}' not found.",
        )
    return StatusResponse(
        job_id=job.id,
        status=job.status,
        progress=job.progress,
        runtime_ms=job.runtime_ms,
        error=job.error,
    )


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Liveness / capability probe",
)
async def health_check() -> HealthResponse:
    """
    Return the service liveness status together with runtime capability flags.

    This endpoint is intentionally lightweight (no DB access) and is suitable
    for use as a Kubernetes liveness or readiness probe.
    """
    return HealthResponse(
        status="ok",
        version="1.0.0",
        java_available=settings.JAVA_AVAILABLE,
        strace_available=settings.STRACE_AVAILABLE,
    )
