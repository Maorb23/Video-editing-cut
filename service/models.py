from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class PublicState(str, Enum):
    uploaded = "uploaded"
    analyzing = "analyzing"
    planning = "planning"
    awaiting_approval = "awaiting_approval"
    approved = "approved"
    rendering = "rendering"
    completed = "completed"
    failed = "failed"


class CreateEditRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    video_id: str = Field(pattern=r"^vid_[0-9a-f]{32}$")
    instruction: str = Field(min_length=1, max_length=20_000)


class RegisterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=8, max_length=1024)
    captcha_token: str | None = Field(default=None, max_length=4096)


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=8, max_length=1024)


class UserResponse(BaseModel):
    id: str
    email: str
    email_verified: bool = False
    avatar_key: str = "camera"
    is_admin: bool = False


class EmailRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    email: str = Field(min_length=3, max_length=320)


class TokenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    token: str = Field(min_length=1, max_length=4096)


class PasswordResetRequest(TokenRequest):
    password: str = Field(min_length=8, max_length=1024)


class AvatarRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    avatar_key: str = Field(pattern=r"^(camera|director|clapperboard|film-reel|video-frame|timeline|lens|play)$")


class TopUpRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    package_key: str = Field(pattern=r"^[a-z0-9_-]{1,32}$")
    simulate: str = Field(default="success", pattern=r"^(success|failure)$")


class CheckoutRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    package_key: str = Field(pattern=r"^[a-z0-9_-]{1,32}$")


class ReviseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    instruction: str = Field(min_length=1, max_length=20_000)


class ApproveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plan_id: str = Field(pattern=r"^pln_[0-9a-f]{32}$")


class VideoResponse(BaseModel):
    id: str
    state: PublicState
    filename: str


class EditResponse(BaseModel):
    id: str
    state: PublicState
    created_at: datetime
    progress: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    iteration: int = 1
    active_iteration: int | None = None
    approved_iteration: int | None = None
    preview_status: str = "queued"
    render_status: str | None = None
    plan_url: str | None = None
    jobs: list[dict[str, Any]] = Field(default_factory=list)


class PlanResponse(BaseModel):
    edit_id: str
    plan_id: str
    status: str
    summary: str
    warnings: list[str]
    edit_plan: dict[str, Any]
    iteration: int = 1
    parent_iteration: int | None = None
    instruction: str = ""
    plan_status: str = "awaiting_approval"
    preview_status: str = "queued"
    render_status: str | None = None
    preview_url: str | None = None
    poster_url: str | None = None
    inspection_url: str | None = None
    video_url: str | None = None
    decision_log: dict[str, Any] = Field(default_factory=lambda: {"observations": [], "decisions": [], "unsupported": [], "assumptions": []})
    error: dict[str, Any] | None = None


class ResultResponse(BaseModel):
    edit_id: str
    state: PublicState
    video_url: str
    artifacts: dict[str, str]
