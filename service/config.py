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
    minimum_silence_seconds: float = .25
    speech_padding_seconds: float = .12
    silence_threshold_min_db: float = -60.
    silence_threshold_max_db: float = -30.
    silence_calibration_margin_db: float = 8.
    silence_fallback_threshold_db: float = -50.
    max_upload_bytes: int = 2 * 1024 * 1024 * 1024
    worker_lease_seconds: int = 300
    worker_max_attempts: int = 3
    # The bundled UI is served over plain HTTP in the documented local setup.
    # Deployments behind HTTPS should opt in with the environment variable.
    session_cookie_secure: bool = False
    django_secret_key: str = "video-editing-local-development-only"
    public_base_url: str = "http://127.0.0.1:8000"
    turnstile_secret_key: str | None = None
    turnstile_site_key: str | None = None
    redis_url: str | None = None
    email_endpoint: str | None = None
    email_api_key: str | None = None
    email_from: str = "Melvid <no-reply@melvid.example>"
    email_provider: str = "generic"

    @classmethod
    def from_env(cls) -> "Settings":
        backend = os.environ.get("VIDEO_EDIT_STORAGE", "filesystem")
        if backend not in {"filesystem", "s3"}:
            raise ValueError("VIDEO_EDIT_STORAGE must be 'filesystem' or 's3'")
        return cls(
            # Railway/Postgres commonly exposes DATABASE_URL; retain the
            # project-specific name as the preferred explicit override.
            database_url=os.environ.get("VIDEO_EDIT_DATABASE_URL") or os.environ.get("DATABASE_URL", ""),
            storage_backend=backend,
            storage_root=Path(os.environ.get("VIDEO_EDIT_STORAGE_ROOT", "./service-data/objects")),
            work_root=Path(os.environ.get("VIDEO_EDIT_WORK_ROOT", "./service-data/jobs")),
            s3_bucket=os.environ.get("VIDEO_EDIT_S3_BUCKET"),
            s3_endpoint_url=os.environ.get("VIDEO_EDIT_S3_ENDPOINT_URL"),
            s3_region=os.environ.get("VIDEO_EDIT_S3_REGION"),
            ffmpeg=os.environ.get("VIDEO_EDIT_FFMPEG"),
            ffprobe=os.environ.get("VIDEO_EDIT_FFPROBE"),
            melt=os.environ.get("VIDEO_EDIT_MELT"),
            minimum_silence_seconds=float(os.environ.get("VIDEO_EDIT_MINIMUM_SILENCE_SECONDS", ".25")),
            speech_padding_seconds=float(os.environ.get("VIDEO_EDIT_SPEECH_PADDING_SECONDS", ".12")),
            silence_threshold_min_db=float(os.environ.get("VIDEO_EDIT_SILENCE_THRESHOLD_MIN_DB", "-60")),
            silence_threshold_max_db=float(os.environ.get("VIDEO_EDIT_SILENCE_THRESHOLD_MAX_DB", "-30")),
            silence_calibration_margin_db=float(os.environ.get("VIDEO_EDIT_SILENCE_CALIBRATION_MARGIN_DB", "8")),
            silence_fallback_threshold_db=float(os.environ.get("VIDEO_EDIT_SILENCE_FALLBACK_THRESHOLD_DB", "-50")),
            max_upload_bytes=int(os.environ.get("VIDEO_EDIT_MAX_UPLOAD_BYTES", str(2 * 1024 * 1024 * 1024))),
            worker_lease_seconds=int(os.environ.get("VIDEO_EDIT_WORKER_LEASE_SECONDS", "300")),
            worker_max_attempts=int(os.environ.get("VIDEO_EDIT_WORKER_MAX_ATTEMPTS", "3")),
            session_cookie_secure=os.environ.get("VIDEO_EDIT_SESSION_COOKIE_SECURE", "false").lower() not in {"0", "false", "no"},
            django_secret_key=os.environ.get("VIDEO_EDIT_DJANGO_SECRET_KEY", "video-editing-local-development-only"),
            public_base_url=os.environ.get("MELVID_PUBLIC_BASE_URL", "http://127.0.0.1:8000").rstrip("/"),
            turnstile_secret_key=os.environ.get("MELVID_TURNSTILE_SECRET_KEY"),
            turnstile_site_key=os.environ.get("MELVID_TURNSTILE_SITE_KEY"),
            redis_url=os.environ.get("REDIS_URL"),
            email_endpoint=os.environ.get("MELVID_EMAIL_ENDPOINT")
            or ("https://api.resend.com/emails" if os.environ.get("EMAIL_PROVIDER", os.environ.get("MELVID_EMAIL_PROVIDER", "generic")).lower() == "resend" else None),
            email_api_key=(os.environ.get("RESEND_API_KEY") or os.environ.get("MELVID_EMAIL_API_KEY") or "").strip() or None,
            email_from=(os.environ.get("DEFAULT_FROM_MAIL") or os.environ.get("MELVID_EMAIL_FROM", "Melvid <no-reply@melvid.example>")).strip(),
            email_provider=(os.environ.get("EMAIL_PROVIDER") or os.environ.get("MELVID_EMAIL_PROVIDER", "generic")).strip(),
        )

    def validate(self) -> None:
        from video_editing.adaptive_silence import SilenceSettings

        SilenceSettings(
            minimum_silence_seconds=self.minimum_silence_seconds,
            speech_padding_seconds=self.speech_padding_seconds,
            threshold_min_db=self.silence_threshold_min_db,
            threshold_max_db=self.silence_threshold_max_db,
            calibration_margin_db=self.silence_calibration_margin_db,
            fallback_threshold_db=self.silence_fallback_threshold_db,
        )
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
        if self.session_cookie_secure and self.django_secret_key == "video-editing-local-development-only":
            raise ValueError("VIDEO_EDIT_DJANGO_SECRET_KEY must be configured for secure deployments")
