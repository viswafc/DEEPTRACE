"""
routers/results.py – Profiling results retrieval endpoint.

Endpoints
---------
GET /api/results/{job_id}
    Return the full ProfileMetrics document for a completed job.
    202 Accepted while the job is still running.
    404 if the job does not exist.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from models.job import JobStatus
from models.metrics import ProfileMetrics
from storage.store import job_store

logger = logging.getLogger(__name__)

router = APIRouter(tags=["results"])


class ResultsErrorResponse(BaseModel):
    """Returned when a result is not yet available (202)."""

    job_id: str
    status: str
    message: str


@router.get(
    "/results/{job_id}",
    summary="Retrieve profiling results for a completed job",
    responses={
        200: {"description": "Full ProfileMetrics payload.", "model": ProfileMetrics},
        202: {
            "description": "Job is still running or queued.",
            "model": ResultsErrorResponse,
        },
        404: {"description": "Job not found."},
    },
)
async def get_results(job_id: str):
    """
    Return the full :class:`~models.metrics.ProfileMetrics` document.

    If the job has not yet completed, a **202 Accepted** response is returned
    with a human-readable status message so clients know to retry later.

    Parameters
    ----------
    job_id: UUID string identifying the profiling job.

    Raises
    ------
    404: If no job with the given ID exists in the store.
    500: If the job is in DONE state but metrics are missing (should never happen).
    """
    job = await job_store.get_job(job_id)
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Job '{job_id}' not found.",
        )

    if job.status in (JobStatus.QUEUED, JobStatus.RUNNING):
        # Return 202 so the client knows to retry
        from fastapi.responses import JSONResponse

        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content={
                "job_id": job.id,
                "status": job.status,
                "message": (
                    "Job is still in progress. "
                    f"Current progress: {job.progress}%."
                ),
            },
        )

    if job.status == JobStatus.ERROR:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "job_id": job.id,
                "status": "error",
                "error": job.error or "Unknown error.",
            },
        )

    # DONE – return metrics
    if not job.metrics:
        logger.error("Job %s is DONE but has no metrics dict.", job_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Job completed but metrics are unavailable.",
        )

    try:
        metrics = ProfileMetrics.model_validate(job.metrics)
    except Exception as exc:
        logger.error("Failed to deserialise metrics for job %s: %s", job_id, exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Metrics deserialization failed.",
        ) from exc

    return metrics
