"""
routers/export.py – Metrics download endpoint.

Endpoints
---------
GET /api/export/{job_id}?format=json|csv
    Download the full profiling metrics for a completed job as either a
    formatted JSON file or a flattened CSV file.
"""

from __future__ import annotations

import csv
import io
import json
import logging
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import Response, StreamingResponse

from models.job import JobStatus
from models.metrics import ProfileMetrics
from storage.store import job_store

logger = logging.getLogger(__name__)

router = APIRouter(tags=["export"])


# ─────────────────────────────────────────────────────────────────────────────
# CSV flattening helpers
# ─────────────────────────────────────────────────────────────────────────────


def _flatten_metrics(metrics: ProfileMetrics) -> list[dict]:
    """
    Flatten a :class:`~models.metrics.ProfileMetrics` object into a list of
    row dictionaries suitable for CSV serialisation.

    Each dictionary represents a logical metric with four keys:
    ``category``, ``metric``, ``value``, and ``unit``.

    Parameters
    ----------
    metrics: The fully-populated ProfileMetrics instance.

    Returns
    -------
    A list of flat dictionaries, one per metric field.
    """
    rows: list[dict] = []

    def row(category: str, metric: str, value, unit: str = "") -> None:
        rows.append(
            {
                "job_id": metrics.job_id,
                "language": metrics.language,
                "category": category,
                "metric": metric,
                "value": value,
                "unit": unit,
            }
        )

    # Top-level
    row("summary", "status", metrics.status)
    row("summary", "runtime_ms", metrics.runtime_ms, "ms")

    # Memory
    row("memory", "peak_mb", metrics.memory.peak_mb, "MB")
    row("memory", "allocations", metrics.memory.allocations, "objects")
    for i, point in enumerate(metrics.memory.timeline):
        row("memory.timeline", f"sample_{i}.t", point.t, "s")
        row("memory.timeline", f"sample_{i}.bytes", point.bytes, "bytes")

    # GC
    row("gc", "total_pause_ms", metrics.gc.total_pause_ms, "ms")
    row("gc", "collections", metrics.gc.collections)
    for i, event in enumerate(metrics.gc.events):
        row("gc.events", f"event_{i}.t", event.t, "s")
        row("gc.events", f"event_{i}.pause_ms", event.pause_ms, "ms")
        row("gc.events", f"event_{i}.type", event.type)

    # Syscalls
    row("syscalls", "total", metrics.syscalls.total)
    row("syscalls", "user_time_pct", metrics.syscalls.user_time_pct, "%")
    row("syscalls", "kernel_time_pct", metrics.syscalls.kernel_time_pct, "%")
    for syscall_name, count in metrics.syscalls.by_type.items():
        row("syscalls.by_type", syscall_name, count, "calls")

    # I/O
    row("io", "reads_bytes", metrics.io.reads_bytes, "bytes")
    row("io", "writes_bytes", metrics.io.writes_bytes, "bytes")
    row("io", "blocked_ms", metrics.io.blocked_ms, "ms")

    # Bottlenecks
    for i, bt in enumerate(metrics.bottlenecks):
        row("bottlenecks", f"bottleneck_{i}.severity", bt.severity)
        row("bottlenecks", f"bottleneck_{i}.type", bt.type)
        row("bottlenecks", f"bottleneck_{i}.message", bt.message)
        if bt.line is not None:
            row("bottlenecks", f"bottleneck_{i}.line", bt.line)

    return rows


# ─────────────────────────────────────────────────────────────────────────────
# Endpoint
# ─────────────────────────────────────────────────────────────────────────────


@router.get(
    "/export/{job_id}",
    summary="Download profiling metrics as JSON or CSV",
    responses={
        200: {"description": "Downloadable metrics file."},
        202: {"description": "Job is still running."},
        404: {"description": "Job not found."},
    },
)
async def export_results(
    job_id: str,
    format: Literal["json", "csv"] = Query(
        default="json",
        description="Export format: 'json' (default) or 'csv'.",
    ),
):
    """
    Export the full profiling result for *job_id* as a downloadable file.

    The ``Content-Disposition`` header instructs browsers to save the response
    rather than display it inline.

    Parameters
    ----------
    job_id: UUID string identifying the profiling job.
    format: ``'json'`` for a pretty-printed JSON file, ``'csv'`` for a
            flattened CSV with one metric per row.

    Raises
    ------
    404: If no job with *job_id* exists.
    202: If the job is still queued or running.
    422: If the job ended with an error.
    """
    job = await job_store.get_job(job_id)
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Job '{job_id}' not found.",
        )

    if job.status in (JobStatus.QUEUED, JobStatus.RUNNING):
        from fastapi.responses import JSONResponse

        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content={
                "job_id": job.id,
                "status": job.status,
                "message": f"Job is still in progress ({job.progress} %).",
            },
        )

    if job.status == JobStatus.ERROR:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "job_id": job.id,
                "status": "error",
                "error": job.error or "Unknown profiling error.",
            },
        )

    if not job.metrics:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Job completed but metrics are unavailable.",
        )

    metrics = ProfileMetrics.model_validate(job.metrics)

    # ── JSON export ──────────────────────────────────────────────────────────
    if format == "json":
        json_bytes = metrics.model_dump_json(indent=2).encode("utf-8")
        filename = f"deeptrace_{job_id}.json"
        return Response(
            content=json_bytes,
            media_type="application/json",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Content-Length": str(len(json_bytes)),
            },
        )

    # ── CSV export ───────────────────────────────────────────────────────────
    rows = _flatten_metrics(metrics)
    buffer = io.StringIO()
    if rows:
        fieldnames = list(rows[0].keys())
        writer = csv.DictWriter(buffer, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    csv_content = buffer.getvalue()
    csv_bytes = csv_content.encode("utf-8")
    filename = f"deeptrace_{job_id}.csv"

    return Response(
        content=csv_bytes,
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Length": str(len(csv_bytes)),
        },
    )
