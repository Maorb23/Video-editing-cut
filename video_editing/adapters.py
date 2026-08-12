"""Provider-neutral semantic-analysis artifact interfaces."""

from __future__ import annotations

from typing import Any

from .errors import Issue, PlanValidationError


def validate_transcript(data: dict[str, Any]) -> dict[str, Any]:
    issues: list[Issue] = []
    if data.get("version") != "1.0":
        issues.append(Issue("unsupported_version", "$.version", "expected '1.0'"))
    segments = data.get("segments")
    if not isinstance(segments, list):
        issues.append(Issue("required", "$.segments", "must be an array"))
    else:
        for index, segment in enumerate(segments):
            path = f"$.segments[{index}]"
            if not isinstance(segment, dict) or not isinstance(segment.get("text"), str):
                issues.append(Issue("invalid_segment", path, "must contain text")); continue
            if not isinstance(segment.get("start_frame"), int) or not isinstance(segment.get("end_frame"), int) or segment["start_frame"] < 0 or segment["end_frame"] <= segment["start_frame"]:
                issues.append(Issue("invalid_range", path, "must have an increasing integer frame range"))
    if issues:
        raise PlanValidationError(issues)
    return data


def validate_visual_analysis(data: dict[str, Any]) -> dict[str, Any]:
    issues: list[Issue] = []
    if data.get("version") != "1.0":
        issues.append(Issue("unsupported_version", "$.version", "expected '1.0'"))
    observations = data.get("observations")
    if not isinstance(observations, list):
        issues.append(Issue("required", "$.observations", "must be an array"))
    else:
        for index, observation in enumerate(observations):
            path = f"$.observations[{index}]"
            if not isinstance(observation, dict) or not isinstance(observation.get("frame"), int) or not isinstance(observation.get("description"), str):
                issues.append(Issue("invalid_observation", path, "must contain integer frame and description"))
    if issues:
        raise PlanValidationError(issues)
    return data
