from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    database_url: str
    storage_backend: str
    storage_root: Path
    work_root: Path
    s3_bucket: str | None = None
    s3_endpoint_url: str | None = None
    s3_region: str | None = None
    ffmpeg: str | None = None
    ffprobe: str | None = None
    melt: str | None = None
    max_upload_bytes: int = 2 * 1024 * 1024 * 1024
    worker_lease_seconds: int = 300
    worker_max_attempts: int = 3

    @classmethod
    def from_env(cls) -> "Settings":
        backend = os.environ.get("VIDEO_EDIT_STORAGE", "filesystem")
        if backend not in {"filesystem", "s3"}:
            raise ValueError("VIDEO_EDIT_STORAGE must be 'filesystem' or 's3'")
        return cls(
            database_url=os.environ.get("VIDEO_EDIT_DATABASE_URL", ""),
            storage_backend=backend,
            storage_root=Path(os.environ.get("VIDEO_EDIT_STORAGE_ROOT", "./service-data/objects")),
            work_root=Path(os.environ.get("VIDEO_EDIT_WORK_ROOT", "./service-data/jobs")),
            s3_bucket=os.environ.get("VIDEO_EDIT_S3_BUCKET"),
            s3_endpoint_url=os.environ.get("VIDEO_EDIT_S3_ENDPOINT_URL"),
            s3_region=os.environ.get("VIDEO_EDIT_S3_REGION"),
            ffmpeg=os.environ.get("VIDEO_EDIT_FFMPEG"),
            ffprobe=os.environ.get("VIDEO_EDIT_FFPROBE"),
            melt=os.environ.get("VIDEO_EDIT_MELT"),
            max_upload_bytes=int(os.environ.get("VIDEO_EDIT_MAX_UPLOAD_BYTES", str(2 * 1024 * 1024 * 1024))),
            worker_lease_seconds=int(os.environ.get("VIDEO_EDIT_WORKER_LEASE_SECONDS", "300")),
            worker_max_attempts=int(os.environ.get("VIDEO_EDIT_WORKER_MAX_ATTEMPTS", "3")),
        )

    def validate(self) -> None:
        if not self.database_url:
            raise ValueError("VIDEO_EDIT_DATABASE_URL is required")
        if self.max_upload_bytes < 1:
            raise ValueError("VIDEO_EDIT_MAX_UPLOAD_BYTES must be positive")
        if self.worker_lease_seconds < 30:
            raise ValueError("VIDEO_EDIT_WORKER_LEASE_SECONDS must be at least 30")
        if not 1 <= self.worker_max_attempts <= 10:
            raise ValueError("VIDEO_EDIT_WORKER_MAX_ATTEMPTS must be between 1 and 10")
        if self.storage_backend == "s3" and not self.s3_bucket:
            raise ValueError("VIDEO_EDIT_S3_BUCKET is required for S3 storage")
