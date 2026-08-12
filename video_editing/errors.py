"""Structured domain errors used by every CLI."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Issue:
    code: str
    path: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "path": self.path, "message": self.message}


class VideoEditingError(Exception):
    """Base error with an actionable message and stable machine code."""

    def __init__(self, message: str, *, code: str = "video_editing_error") -> None:
        super().__init__(message)
        self.code = code


class PlanValidationError(VideoEditingError):
    def __init__(self, issues: list[Issue]) -> None:
        super().__init__(f"edit plan has {len(issues)} validation error(s)", code="invalid_edit_plan")
        self.issues = issues


class ExternalToolError(VideoEditingError):
    pass
