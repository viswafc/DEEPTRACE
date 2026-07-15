"""
config.py – Centralised settings for the DeepTrace profiler backend.

All values can be overridden with environment variables (prefix: DEEPTRACE_).
"""

import shutil
from pathlib import Path
from typing import List

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _detect_strace() -> bool:
    """Return True when the 'strace' binary is present on PATH."""
    return shutil.which("strace") is not None


def _detect_java() -> bool:
    """Return True when both 'java' and 'javac' are present on PATH."""
    return shutil.which("java") is not None and shutil.which("javac") is not None


class Settings(BaseSettings):
    """Application-wide settings loaded from environment / .env file."""

    model_config = SettingsConfigDict(
        env_prefix="DEEPTRACE_",
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # ------------------------------------------------------------------ paths
    BASE_DIR: Path = Field(
        default_factory=lambda: Path(__file__).resolve().parent,
        description="Absolute path to the backend package directory.",
    )
    JOBS_DIR: Path = Field(
        default=Path("/tmp/deeptrace_jobs"),
        description="Directory used to persist job JSON files.",
    )

    # --------------------------------------------------------- resource limits
    MAX_CONCURRENT_JOBS: int = Field(
        default=5,
        ge=1,
        description="Maximum number of profiling jobs that may run in parallel.",
    )
    PYTHON_TIMEOUT: int = Field(
        default=30,
        ge=1,
        description="Wall-clock timeout (seconds) for Python profiling runs.",
    )
    JAVA_TIMEOUT: int = Field(
        default=60,
        ge=1,
        description="Wall-clock timeout (seconds) for Java profiling runs.",
    )
    MAX_MEMORY_MB: int = Field(
        default=512,
        ge=64,
        description="Soft RSS memory limit (MB) applied to child processes.",
    )

    # ------------------------------------------------------------ feature flags
    ALLOWED_LANGUAGES: List[str] = Field(
        default=["python", "java"],
        description="Languages accepted by the /api/submit endpoint.",
    )

    # ------------------------------------------------------------- CORS / auth
    CORS_ORIGINS: List[str] = Field(
        default=["*"],
        description="List of allowed CORS origins.  Use ['*'] to allow all.",
    )

    # -------------------------------------------------------------- observability
    LOG_LEVEL: str = Field(
        default="INFO",
        description="Python logging level (DEBUG, INFO, WARNING, ERROR).",
    )

    # -------------------------------------------------- auto-detected at import
    STRACE_AVAILABLE: bool = Field(
        default_factory=_detect_strace,
        description="True when the strace binary is found on PATH.",
    )
    JAVA_AVAILABLE: bool = Field(
        default_factory=_detect_java,
        description="True when java + javac binaries are found on PATH.",
    )


# Module-level singleton – import this everywhere.
settings = Settings()
