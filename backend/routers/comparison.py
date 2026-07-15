"""
routers/comparison.py – Side-by-side job comparison endpoint.

Endpoints
---------
POST /api/compare
    Accept two job IDs, fetch their metrics, compute per-field deltas,
    and return a structured comparison document.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from models.job import JobStatus
from models.metrics import ProfileMetrics
from storage.store import job_store

logger = logging.getLogger(__name__)

router = APIRouter(tags=["comparison"])


# ─────────────────────────────────────────────────────────────────────────────
# Schemas
# ─────────────────────────────────────────────────────────────────────────────


class CompareRequest(BaseModel):
    """Body schema for POST /api/compare."""

    job_id_a: str
    job_id_b: str


class MetricPair(BaseModel):
    """A pair of values for the same metric from two jobs."""

    a: float
    b: float
    delta: float
    delta_pct: Optional[float]


class MemoryDelta(BaseModel):
    peak_mb: MetricPair
    allocations: MetricPair


class GCDelta(BaseModel):
    total_pause_ms: MetricPair
    collections: MetricPair


class SyscallDelta(BaseModel):
    total: MetricPair
    user_time_pct: MetricPair
    kernel_time_pct: MetricPair


class IODelta(BaseModel):
    reads_bytes: MetricPair
    writes_bytes: MetricPair
    blocked_ms: MetricPair


class ComparisonDeltas(BaseModel):
    runtime_ms: MetricPair
    memory: MemoryDelta
    gc: GCDelta
    syscalls: SyscallDelta
    io: IODelta


class CompareResponse(BaseModel):
    """Full comparison response document."""

    metrics_a: ProfileMetrics
    metrics_b: ProfileMetrics
    deltas: ComparisonDeltas


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _pair(a: float, b: float) -> MetricPair:
    """Build a MetricPair with absolute and percentage delta."""
    delta = b - a
    delta_pct = round((delta / a * 100), 2) if a != 0 else None
    return MetricPair(a=a, b=b, delta=round(delta, 4), delta_pct=delta_pct)


async def _resolve_metrics(job_id: str, label: str) -> ProfileMetrics:
    """
    Fetch and validate metrics for a job, raising appropriate HTTP errors.

    Parameters
    ----------
    job_id: Job UUID to look up.
    label:  Human-readable label used in error messages ('A' or 'B').

    Returns
    -------
    Validated :class:`~models.metrics.ProfileMetrics` instance.

    Raises
    ------
    404: Job not found.
    409: Job is not in DONE state.
    """
    job = await job_store.get_job(job_id)
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Job {label} ('{job_id}') not found.",
        )
    if job.status != JobStatus.DONE:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Job {label} ('{job_id}') is not complete yet "
                f"(current status: '{job.status}')."
            ),
        )
    if not job.metrics:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Job {label} ('{job_id}') is DONE but has no metrics.",
        )
    return ProfileMetrics.model_validate(job.metrics)


# ─────────────────────────────────────────────────────────────────────────────
# Endpoint
# ─────────────────────────────────────────────────────────────────────────────


@router.post(
    "/compare",
    response_model=CompareResponse,
    summary="Compare metrics from two profiling jobs",
)
async def compare_jobs(body: CompareRequest) -> CompareResponse:
    """
    Fetch and diff the profiling results of two completed jobs.

    For each major metric the response includes the raw values from both runs
    and a computed delta (``b - a``) plus an optional percentage change.

    Parameters
    ----------
    body.job_id_a: UUID of the baseline job.
    body.job_id_b: UUID of the comparison job.

    Raises
    ------
    404: If either job does not exist.
    409: If either job has not yet finished.
    400: If both job IDs are the same (comparison would be trivial).
    """
    if body.job_id_a == body.job_id_b:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="job_id_a and job_id_b must be different jobs.",
        )

    metrics_a = await _resolve_metrics(body.job_id_a, "A")
    metrics_b = await _resolve_metrics(body.job_id_b, "B")

    deltas = ComparisonDeltas(
        runtime_ms=_pair(metrics_a.runtime_ms, metrics_b.runtime_ms),
        memory=MemoryDelta(
            peak_mb=_pair(metrics_a.memory.peak_mb, metrics_b.memory.peak_mb),
            allocations=_pair(
                float(metrics_a.memory.allocations),
                float(metrics_b.memory.allocations),
            ),
        ),
        gc=GCDelta(
            total_pause_ms=_pair(
                metrics_a.gc.total_pause_ms, metrics_b.gc.total_pause_ms
            ),
            collections=_pair(
                float(metrics_a.gc.collections), float(metrics_b.gc.collections)
            ),
        ),
        syscalls=SyscallDelta(
            total=_pair(
                float(metrics_a.syscalls.total), float(metrics_b.syscalls.total)
            ),
            user_time_pct=_pair(
                metrics_a.syscalls.user_time_pct, metrics_b.syscalls.user_time_pct
            ),
            kernel_time_pct=_pair(
                metrics_a.syscalls.kernel_time_pct,
                metrics_b.syscalls.kernel_time_pct,
            ),
        ),
        io=IODelta(
            reads_bytes=_pair(
                float(metrics_a.io.reads_bytes), float(metrics_b.io.reads_bytes)
            ),
            writes_bytes=_pair(
                float(metrics_a.io.writes_bytes), float(metrics_b.io.writes_bytes)
            ),
            blocked_ms=_pair(metrics_a.io.blocked_ms, metrics_b.io.blocked_ms),
        ),
    )

    return CompareResponse(
        metrics_a=metrics_a,
        metrics_b=metrics_b,
        deltas=deltas,
    )
