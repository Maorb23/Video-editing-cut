"""Deterministically resolve structural operations into timeline clips."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from .errors import VideoEditingError


STRUCTURAL = {"trim", "split", "remove", "insert", "reorder"}


def resolve_timeline(plan: dict[str, Any]) -> list[dict[str, Any]]:
    tracks = deepcopy(plan["tracks"])

    def all_clip_ids() -> set[str]:
        return {clip["id"] for track in tracks for clip in track["clips"]}

    def locate(clip_id: str) -> tuple[dict[str, Any], int, dict[str, Any]]:
        for track in tracks:
            for index, clip in enumerate(track["clips"]):
                if clip["id"] == clip_id:
                    return track, index, clip
        raise VideoEditingError(f"operation target no longer exists: {clip_id}", code="missing_target")

    for operation in plan.get("operations", []):
        if not operation.get("enabled", True) or operation["type"] not in STRUCTURAL:
            continue
        kind = operation["type"]
        if kind == "insert":
            target_track = next((t for t in tracks if t["id"] == operation["track_id"]), None)
            if target_track is None:
                raise VideoEditingError(f"insert references unknown track: {operation['track_id']}", code="missing_target")
            if operation["clip"]["id"] in all_clip_ids():
                raise VideoEditingError(f"insert creates duplicate clip ID: {operation['clip']['id']}", code="duplicate_id")
            target_track["clips"].append(deepcopy(operation["clip"]))
            continue
        if kind == "reorder":
            target_track = next((t for t in tracks if t["id"] == operation["track_id"]), None)
            if target_track is None:
                raise VideoEditingError(f"reorder references unknown track: {operation['track_id']}", code="missing_target")
            by_id = {clip["id"]: clip for clip in target_track["clips"]}
            if set(operation["clip_ids"]) != set(by_id):
                raise VideoEditingError("reorder clip_ids must contain every track clip exactly once", code="invalid_reorder")
            cursor = 0
            reordered = []
            for clip_id in operation["clip_ids"]:
                clip = by_id[clip_id]
                clip["timeline_start"] = cursor
                cursor += clip["duration"]
                reordered.append(clip)
            target_track["clips"] = reordered
            continue
        track, index, clip = locate(operation["target"])
        if kind == "remove":
            del track["clips"][index]
        elif kind == "trim":
            source_in = operation.get("source_in", clip["source_in"])
            duration = operation.get("duration", clip["duration"])
            if source_in < 0 or duration <= 0:
                raise VideoEditingError("trim creates an invalid source range", code="invalid_range")
            clip["source_in"] = source_in
            clip["duration"] = duration
        elif kind == "split":
            at = operation["at"]
            relative = at - clip["timeline_start"]
            if relative <= 0 or relative >= clip["duration"]:
                raise VideoEditingError(f"split {operation['id']} must fall strictly inside its clip", code="invalid_range")
            right = deepcopy(clip)
            right["id"] = f"{clip['id']}__{operation['id']}"
            if right["id"] in all_clip_ids():
                raise VideoEditingError(f"split creates duplicate clip ID: {right['id']}", code="duplicate_id")
            right["timeline_start"] = at
            right["source_in"] += relative
            right["duration"] -= relative
            clip["duration"] = relative
            track["clips"].insert(index + 1, right)
    for track in tracks:
        track["clips"].sort(key=lambda clip: (clip["timeline_start"], clip["id"]))
    return tracks
