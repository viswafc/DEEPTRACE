"""
main.py – FastAPI application entry point for DeepTrace.

Features
--------
* CORS middleware configured from settings.
* All API routers mounted under /api.
* WebSocket endpoint /ws/{job_id} for real-time job updates.
* Startup: initialise JobStore, create JOBS_DIR, schedule periodic cleanup.
* Custom 404 / 500 exception handlers returning JSON.
* Uvicorn startup block for ``python main.py``.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator

import uvicorn
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from config import settings
from models.job import JobStatus
from routers import comparison, export, results, submission
from storage.store import job_store

# ─────────────────────────────────────────────────────────────────────────────
# Logging setup
# ─────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=settings.LOG_LEVEL.upper(),
    format="%(asctime)s [%(levelname)s] %(name)s – %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("deeptrace")


# ─────────────────────────────────────────────────────────────────────────────
# WebSocket connection manager
# ─────────────────────────────────────────────────────────────────────────────


class ConnectionManager:
    """
    Manages active WebSocket connections keyed by job_id.

    Each job may have multiple concurrent subscribers.  When a job update
    is published via :meth:`broadcast`, all connections for that job_id
    receive the message.
    """

    def __init__(self) -> None:
        self._subscribers: dict[str, list[asyncio.Queue]] = {}
        self._lock = asyncio.Lock()

    async def subscribe(self, job_id: str) -> asyncio.Queue:
        """
        Create a new subscription queue for *job_id* and return it.

        Parameters
        ----------
        job_id: The job to subscribe to.

        Returns
        -------
        An :class:`asyncio.Queue` that will receive update dicts.
        """
        queue: asyncio.Queue = asyncio.Queue(maxsize=64)
        async with self._lock:
            self._subscribers.setdefault(job_id, []).append(queue)
        return queue

    async def unsubscribe(self, job_id: str, queue: asyncio.Queue) -> None:
        """Remove a subscription queue for *job_id*."""
        async with self._lock:
            subs = self._subscribers.get(job_id, [])
            try:
                subs.remove(queue)
            except ValueError:
                pass
            if not subs:
                self._subscribers.pop(job_id, None)

    async def broadcast(self, job_id: str, payload: dict) -> None:
        """
        Push *payload* to all subscribers of *job_id*.

        Parameters
        ----------
        job_id:  Target job identifier.
        payload: JSON-serialisable dictionary to deliver.
        """
        async with self._lock:
            queues = list(self._subscribers.get(job_id, []))
        for queue in queues:
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                logger.warning(
                    "WebSocket queue full for job %s – dropping message.", job_id
                )


ws_manager = ConnectionManager()


# ─────────────────────────────────────────────────────────────────────────────
# Background poller that drives WebSocket updates
# ─────────────────────────────────────────────────────────────────────────────


async def _poll_job_and_broadcast(job_id: str) -> None:
    """
    Continuously poll job state and broadcast updates to WebSocket subscribers.

    Exits when the job reaches a terminal state (DONE or ERROR).

    Parameters
    ----------
    job_id: The job to watch.
    """
    last_progress = -1
    last_status = ""

    while True:
        job = await job_store.get_job(job_id)
        if job is None:
            await ws_manager.broadcast(
                job_id,
                {"type": "error", "job_id": job_id, "message": "Job not found."},
            )
            return

        if job.progress != last_progress or job.status != last_status:
            last_progress = job.progress
            last_status = job.status

            update: dict = {
                "type": "progress",
                "job_id": job_id,
                "status": job.status,
                "progress": job.progress,
                "runtime_ms": job.runtime_ms,
                "error": job.error,
            }

            if job.status == JobStatus.DONE and job.metrics:
                update["type"] = "completed"
                update["metrics"] = job.metrics
            elif job.status == JobStatus.ERROR:
                update["type"] = "error"

            await ws_manager.broadcast(job_id, update)

            if job.status in (JobStatus.DONE, JobStatus.ERROR):
                return

        await asyncio.sleep(0.5)


# ─────────────────────────────────────────────────────────────────────────────
# Periodic cleanup task
# ─────────────────────────────────────────────────────────────────────────────


async def _cleanup_loop() -> None:
    """Run job store cleanup every 10 minutes indefinitely."""
    while True:
        await asyncio.sleep(600)
        try:
            count = await job_store.cleanup_old_jobs()
            if count:
                logger.info("Periodic cleanup: removed %d expired jobs.", count)
        except Exception as exc:  # noqa: BLE001
            logger.error("Cleanup loop error: %s", exc)


# ─────────────────────────────────────────────────────────────────────────────
# Application lifespan
# ─────────────────────────────────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application startup / shutdown context manager."""
    settings.JOBS_DIR.mkdir(parents=True, exist_ok=True)
    await job_store.initialise()
    cleanup_task = asyncio.create_task(_cleanup_loop())

    logger.info("DeepTrace backend starting up.")
    logger.info("  JOBS_DIR           : %s", settings.JOBS_DIR)
    logger.info("  MAX_CONCURRENT_JOBS: %d", settings.MAX_CONCURRENT_JOBS)
    logger.info("  ALLOWED_LANGUAGES  : %s", settings.ALLOWED_LANGUAGES)
    logger.info("  JAVA_AVAILABLE     : %s", settings.JAVA_AVAILABLE)
    logger.info("  STRACE_AVAILABLE   : %s", settings.STRACE_AVAILABLE)
    logger.info("  LOG_LEVEL          : %s", settings.LOG_LEVEL)

    yield

    cleanup_task.cancel()
    try:
        await cleanup_task
    except asyncio.CancelledError:
        pass
    logger.info("DeepTrace backend shut down.")


# ─────────────────────────────────────────────────────────────────────────────
# FastAPI application
# ─────────────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="DeepTrace Code Profiler",
    description=(
        "A backend service that accepts Python and Java source code snippets, "
        "profiles their runtime behaviour, and returns structured performance "
        "metrics including memory usage, GC events, syscall statistics, and I/O."
    ),
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

# ── CORS ─────────────────────────────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Routers ───────────────────────────────────────────────────────────────────
app.include_router(submission.router, prefix="/api")
app.include_router(results.router, prefix="/api")
app.include_router(comparison.router, prefix="/api")
app.include_router(export.router, prefix="/api")


# ─────────────────────────────────────────────────────────────────────────────
# Custom exception handlers
# ─────────────────────────────────────────────────────────────────────────────


@app.exception_handler(404)
async def not_found_handler(request: Request, exc) -> JSONResponse:
    """Return a JSON 404 response for unmatched routes."""
    return JSONResponse(
        status_code=status.HTTP_404_NOT_FOUND,
        content={
            "error": "Not Found",
            "detail": f"The path '{request.url.path}' does not exist.",
        },
    )


@app.exception_handler(500)
async def internal_error_handler(request: Request, exc) -> JSONResponse:
    """Return a sanitised JSON 500 response, logging the full traceback."""
    logger.exception("Unhandled server error on %s", request.url.path)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "error": "Internal Server Error",
            "detail": "An unexpected error occurred. Please try again later.",
        },
    )


# ─────────────────────────────────────────────────────────────────────────────
# WebSocket endpoint
# ─────────────────────────────────────────────────────────────────────────────

_WS_PING_INTERVAL = 5.0  # seconds


@app.websocket("/ws/{job_id}")
async def websocket_job_updates(websocket: WebSocket, job_id: str) -> None:
    """
    Stream real-time profiling progress for *job_id* over a WebSocket.

    Protocol
    --------
    * On connect, an immediate ``"connected"`` message is sent.
    * Every 5 s a ``"ping"`` frame keeps the connection alive.
    * ``"progress"`` frames: ``{type, job_id, status, progress, runtime_ms, error}``.
    * On completion: ``"completed"`` frame with ``metrics`` attached.
    * On error: ``"error"`` frame with ``error`` field.
    * After the terminal frame the server closes the connection.

    Parameters
    ----------
    websocket: The WebSocket connection injected by FastAPI.
    job_id:    The job to subscribe to.
    """
    await websocket.accept()
    logger.debug("WebSocket client connected for job %s", job_id)

    job = await job_store.get_job(job_id)
    if job is None:
        await websocket.send_json(
            {"type": "error", "job_id": job_id, "message": "Job not found."}
        )
        await websocket.close(code=4004)
        return

    queue = await ws_manager.subscribe(job_id)

    # If job is already in terminal state, deliver final frame immediately
    if job.status in (JobStatus.DONE, JobStatus.ERROR):
        payload: dict = {
            "type": "completed" if job.status == JobStatus.DONE else "error",
            "job_id": job_id,
            "status": job.status,
            "progress": job.progress,
            "runtime_ms": job.runtime_ms,
            "error": job.error,
        }
        if job.status == JobStatus.DONE and job.metrics:
            payload["metrics"] = job.metrics
        await websocket.send_json(payload)
        await ws_manager.unsubscribe(job_id, queue)
        await websocket.close()
        return

    # Send initial connected message
    await websocket.send_json(
        {
            "type": "connected",
            "job_id": job_id,
            "status": job.status,
            "progress": job.progress,
        }
    )

    poller_task = asyncio.create_task(_poll_job_and_broadcast(job_id))

    try:
        while True:
            try:
                message = await asyncio.wait_for(
                    queue.get(), timeout=_WS_PING_INTERVAL
                )
                await websocket.send_json(message)
                if message.get("type") in ("completed", "error"):
                    break
            except asyncio.TimeoutError:
                try:
                    await websocket.send_json({"type": "ping", "job_id": job_id})
                except Exception:
                    break
    except WebSocketDisconnect:
        logger.debug("WebSocket client disconnected for job %s", job_id)
    except Exception as exc:  # noqa: BLE001
        logger.error("WebSocket error for job %s: %s", job_id, exc)
    finally:
        poller_task.cancel()
        try:
            await poller_task
        except asyncio.CancelledError:
            pass
        await ws_manager.unsubscribe(job_id, queue)
        try:
            await websocket.close()
        except Exception:
            pass
        logger.debug("WebSocket handler finished for job %s", job_id)


# ─────────────────────────────────────────────────────────────────────────────
# Direct invocation
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=False,
        log_level=settings.LOG_LEVEL.lower(),
        access_log=True,
    )
