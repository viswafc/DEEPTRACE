"""
storage/store.py – Thread-safe, async-aware job store for DeepTrace.

Jobs are kept in an in-memory dictionary for fast access and also persisted as
individual JSON files under JOBS_DIR so they survive server restarts.
An automatic cleanup task removes jobs that are older than one hour.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

import aiofiles

from config import settings
from models.job import Job, JobStatus

logger = logging.getLogger(__name__)

# Maximum age of a completed/errored job before it is purged automatically.
_JOB_TTL = timedelta(hours=1)


class JobStore:
    """
    Persistent, thread-safe store for profiling jobs.

    All public methods are coroutines and must be awaited.  Internal state is
    protected by a single :class:`asyncio.Lock`; callers should therefore
    never hold the lock themselves.

    Jobs are stored as ``<JOBS_DIR>/<job_id>.json`` files so they survive
    process restarts.  On startup call :meth:`load_persisted_jobs` once to
    hydrate the in-memory cache from disk.
    """

    def __init__(self) -> None:
        self._jobs: Dict[str, Job] = {}
        self._lock: asyncio.Lock = asyncio.Lock()
        self._jobs_dir: Path = settings.JOBS_DIR

    # ---------------------------------------------------------------- lifecycle

    async def initialise(self) -> None:
        """
        Create the jobs directory and hydrate in-memory state from disk.

        Call this once during application startup.
        """
        self._jobs_dir.mkdir(parents=True, exist_ok=True)
        await self._load_persisted_jobs()
        logger.info("JobStore initialised.  JOBS_DIR=%s", self._jobs_dir)

    # -------------------------------------------------------------- CRUD helpers

    async def create_job(self, language: str, code: str) -> Job:
        """
        Create a new job in QUEUED state, persist it, and return it.

        Parameters
        ----------
        language: The programming language of the submitted code.
        code:     The source code to profile.

        Returns
        -------
        The newly created :class:`~models.job.Job` instance.
        """
        job = Job(id=str(uuid.uuid4()), language=language, code=code)
        async with self._lock:
            self._jobs[job.id] = job
        await self._persist(job)
        logger.debug("Created job %s (%s)", job.id, language)
        return job

    async def get_job(self, job_id: str) -> Optional[Job]:
        """
        Retrieve a job by its identifier.

        Parameters
        ----------
        job_id: UUID string identifying the job.

        Returns
        -------
        The :class:`~models.job.Job` if found, otherwise *None*.
        """
        async with self._lock:
            return self._jobs.get(job_id)

    async def update_job(self, job_id: str, **kwargs) -> Optional[Job]:
        """
        Apply arbitrary field updates to an existing job and persist it.

        Only fields that are valid :class:`~models.job.Job` attributes will
        be accepted; unknown keys are silently ignored.

        Parameters
        ----------
        job_id: UUID string identifying the job.
        **kwargs: Field name → new value pairs to apply.

        Returns
        -------
        The updated :class:`~models.job.Job`, or *None* if not found.
        """
        async with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                logger.warning("update_job: job %s not found", job_id)
                return None
            valid_fields = job.model_fields.keys()
            for key, value in kwargs.items():
                if key in valid_fields:
                    setattr(job, key, value)
        await self._persist(job)
        return job

    async def list_jobs(self) -> List[Job]:
        """
        Return all jobs currently held in the store.

        Returns
        -------
        A snapshot list of :class:`~models.job.Job` objects.
        """
        async with self._lock:
            return list(self._jobs.values())

    async def delete_job(self, job_id: str) -> bool:
        """
        Remove a job from memory and delete its persisted JSON file.

        Parameters
        ----------
        job_id: UUID string identifying the job.

        Returns
        -------
        *True* if the job was found and removed, *False* otherwise.
        """
        async with self._lock:
            job = self._jobs.pop(job_id, None)
        if job is None:
            return False
        path = self._job_path(job_id)
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            logger.error("Failed to delete job file %s: %s", path, exc)
        logger.debug("Deleted job %s", job_id)
        return True

    # ------------------------------------------------------------ auto-cleanup

    async def cleanup_old_jobs(self) -> int:
        """
        Delete all jobs whose *completed_at* timestamp is older than TTL (1 h).

        Returns
        -------
        Number of jobs that were purged.
        """
        cutoff = datetime.now(timezone.utc) - _JOB_TTL
        async with self._lock:
            to_delete = [
                jid
                for jid, job in self._jobs.items()
                if job.completed_at is not None and job.completed_at < cutoff
            ]
        count = 0
        for jid in to_delete:
            removed = await self.delete_job(jid)
            if removed:
                count += 1
        if count:
            logger.info("Cleaned up %d expired jobs.", count)
        return count

    # ----------------------------------------------------------------- private

    def _job_path(self, job_id: str) -> Path:
        """Return the filesystem path for the given job's JSON file."""
        return self._jobs_dir / f"{job_id}.json"

    async def _persist(self, job: Job) -> None:
        """Write a job to its JSON file asynchronously."""
        path = self._job_path(job.id)
        try:
            data = job.model_dump_json(indent=2)
            async with aiofiles.open(path, "w", encoding="utf-8") as fh:
                await fh.write(data)
        except OSError as exc:
            logger.error("Failed to persist job %s: %s", job.id, exc)

    async def _load_persisted_jobs(self) -> None:
        """Scan JOBS_DIR and load all valid job JSON files into memory."""
        loaded = 0
        for path in self._jobs_dir.glob("*.json"):
            try:
                async with aiofiles.open(path, "r", encoding="utf-8") as fh:
                    raw = await fh.read()
                job = Job.model_validate_json(raw)
                self._jobs[job.id] = job
                loaded += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning("Skipping invalid job file %s: %s", path.name, exc)
        logger.info("Loaded %d persisted jobs from disk.", loaded)


# Module-level singleton shared across the application.
job_store = JobStore()
