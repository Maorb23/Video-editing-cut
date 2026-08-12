"""Strict loading and semantic validation for edit-plan version 1."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import Issue, PlanValidationError, VideoEditingError
from .filters import FILTERS
from .timebase import frame_rate


PLAN_VERSION = "1.0"
OP_TYPES = {
    "trim", "split", "remove", "insert", "reorder", "transition", "caption",
    "overlay", "transform", "volume", "fade_audio", "audio_mix", "speed",
    "chroma_key", "mask", "filter",
}

OP_FIELDS: dict[str, set[str]] = {
    "trim": {"source_in", "duration"},
    "split": {"at"},
    "remove": set(),
    "insert": {"track_id", "clip"},
    "reorder": {"track_id", "clip_ids"},
    "transition": {"from_clip_id", "to_clip_id", "kind", "duration", "track_id"},
    "caption": {"text", "font", "size", "color", "background", "geometry", "halign", "valign"},
    "overlay": {"asset_id", "geometry", "opacity", "track_id"},
    "transform": {"geometry", "rotation", "opacity", "keyframes", "interpolation"},
    "volume": {"gain_db", "level"},
    "fade_audio": {"direction", "duration", "from", "to"},
    "audio_mix": {"gain_db", "pan"},
    "speed": {"factor"},
    "chroma_key": {"color", "variance", "edge"},
    "mask": {"resource", "mode", "softness", "invert"},
    "filter": {"name", "properties"},
}

OP_REQUIRED: dict[str, set[str]] = {
    "trim": set(), "split": {"at"}, "remove": set(), "insert": {"track_id", "clip"},
    "reorder": {"track_id", "clip_ids"},
    "transition": {"from_clip_id", "to_clip_id", "duration"},
    "caption": {"text", "duration"}, "overlay": {"asset_id", "duration"},
    "transform": set(), "volume": set(), "fade_audio": {"direction", "duration"},
    "audio_mix": set(), "speed": {"factor"}, "chroma_key": set(),
    "mask": {"resource"}, "filter": {"name", "properties"},
}

COMMON_OP_FIELDS = {"id", "type", "target", "start", "duration", "enabled"}
TOP_FIELDS = {"version", "profile", "assets", "tracks", "operations", "export", "transcript", "analysis"}
PROFILE_FIELDS = {"width", "height", "frame_rate", "sample_rate", "channels", "progressive", "colorspace"}
ASSET_FIELDS = {"id", "path", "kind", "duration_frames", "fingerprint", "probe"}
TRACK_FIELDS = {"id", "kind", "name", "clips", "muted", "hidden"}
CLIP_FIELDS = {"id", "asset_id", "timeline_start", "source_in", "duration", "enabled"}
EXPORT_FIELDS = {"format", "video_codec", "audio_codec", "video_bitrate", "audio_bitrate", "pixel_format", "movflags"}


@dataclass(frozen=True)
class ValidatedPlan:
    data: dict[str, Any]
    source: Path

    @property
    def profile(self) -> dict[str, Any]:
        return self.data["profile"]


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise VideoEditingError(f"file not found: {path}", code="missing_file") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise VideoEditingError(f"cannot read JSON {path}: {exc}", code="invalid_json") from exc
    if not isinstance(value, dict):
        raise VideoEditingError(f"JSON root must be an object: {path}", code="invalid_json")
    return value


def _unknown(obj: dict[str, Any], allowed: set[str], path: str, issues: list[Issue]) -> None:
    for key in sorted(set(obj) - allowed):
        issues.append(Issue("unknown_property", f"{path}.{key}", "property is not supported"))


def _positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _frame(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def validate_plan(data: dict[str, Any], *, source: Path = Path("edit-plan.json"), check_files: bool = True) -> ValidatedPlan:
    issues: list[Issue] = []
    _unknown(data, TOP_FIELDS, "$", issues)
    if data.get("version") != PLAN_VERSION:
        issues.append(Issue("unsupported_version", "$.version", f"expected {PLAN_VERSION!r}"))

    profile = data.get("profile")
    if not isinstance(profile, dict):
        issues.append(Issue("required", "$.profile", "profile must be an object"))
        profile = {}
    else:
        _unknown(profile, PROFILE_FIELDS, "$.profile", issues)
    for key in ("width", "height", "sample_rate", "channels"):
        if not _positive_int(profile.get(key)):
            issues.append(Issue("invalid_profile", f"$.profile.{key}", "must be a positive integer"))
    rate = profile.get("frame_rate")
    if not isinstance(rate, dict) or set(rate) != {"numerator", "denominator"}:
        issues.append(Issue("invalid_profile", "$.profile.frame_rate", "must contain only positive numerator and denominator"))
    elif not _positive_int(rate["numerator"]) or not _positive_int(rate["denominator"]):
        issues.append(Issue("invalid_profile", "$.profile.frame_rate", "values must be positive integers"))
    else:
        try:
            frame_rate(rate["numerator"], rate["denominator"])
        except VideoEditingError as exc:
            issues.append(Issue(exc.code, "$.profile.frame_rate", str(exc)))

    assets = data.get("assets")
    if not isinstance(assets, list):
        issues.append(Issue("required", "$.assets", "assets must be an array"))
        assets = []
    asset_ids: set[str] = set()
    asset_lookup: dict[str, dict[str, Any]] = {}
    base = source.resolve().parent
    for index, asset in enumerate(assets):
        p = f"$.assets[{index}]"
        if not isinstance(asset, dict):
            issues.append(Issue("invalid_type", p, "asset must be an object")); continue
        _unknown(asset, ASSET_FIELDS, p, issues)
        asset_id = asset.get("id")
        if not isinstance(asset_id, str) or not asset_id:
            issues.append(Issue("required", f"{p}.id", "must be a nonempty string"))
        elif asset_id in asset_ids:
            issues.append(Issue("duplicate_id", f"{p}.id", "asset ID is duplicated"))
        else:
            asset_ids.add(asset_id)
            asset_lookup[asset_id] = asset
        path_value = asset.get("path")
        if not isinstance(path_value, str) or not path_value:
            issues.append(Issue("required", f"{p}.path", "must be a nonempty string"))
        elif check_files and not (base / path_value).resolve().is_file():
            issues.append(Issue("missing_asset", f"{p}.path", f"asset does not exist: {path_value}"))
        if asset.get("kind") not in {"video", "audio", "image"}:
            issues.append(Issue("invalid_asset", f"{p}.kind", "must be video, audio, or image"))
        if not _positive_int(asset.get("duration_frames")):
            issues.append(Issue("invalid_asset", f"{p}.duration_frames", "must be a positive integer"))

    tracks = data.get("tracks")
    if not isinstance(tracks, list) or not tracks:
        issues.append(Issue("required", "$.tracks", "tracks must be a nonempty array"))
        tracks = []
    track_ids: set[str] = set()
    clip_ids: set[str] = set()
    clip_tracks: dict[str, str] = {}
    for ti, track in enumerate(tracks):
        tp = f"$.tracks[{ti}]"
        if not isinstance(track, dict):
            issues.append(Issue("invalid_type", tp, "track must be an object")); continue
        _unknown(track, TRACK_FIELDS, tp, issues)
        track_id = track.get("id")
        if not isinstance(track_id, str) or not track_id:
            issues.append(Issue("required", f"{tp}.id", "must be a nonempty string"))
        elif track_id in track_ids:
            issues.append(Issue("duplicate_id", f"{tp}.id", "track ID is duplicated"))
        else:
            track_ids.add(track_id)
        if track.get("kind") not in {"video", "audio"}:
            issues.append(Issue("invalid_track", f"{tp}.kind", "must be video or audio"))
        clips = track.get("clips")
        if not isinstance(clips, list):
            issues.append(Issue("required", f"{tp}.clips", "must be an array")); continue
        spans: list[tuple[int, int, str]] = []
        for ci, clip in enumerate(clips):
            cp = f"{tp}.clips[{ci}]"
            if not isinstance(clip, dict):
                issues.append(Issue("invalid_type", cp, "clip must be an object")); continue
            _unknown(clip, CLIP_FIELDS, cp, issues)
            cid = clip.get("id")
            if not isinstance(cid, str) or not cid:
                issues.append(Issue("required", f"{cp}.id", "must be a nonempty string"))
            elif cid in clip_ids:
                issues.append(Issue("duplicate_id", f"{cp}.id", "clip ID is duplicated"))
            else:
                clip_ids.add(cid)
                clip_tracks[cid] = str(track_id)
            if clip.get("asset_id") not in asset_ids:
                issues.append(Issue("missing_asset", f"{cp}.asset_id", "does not identify a declared asset"))
            for key in ("timeline_start", "source_in"):
                if not _frame(clip.get(key)):
                    issues.append(Issue("invalid_range", f"{cp}.{key}", "must be a nonnegative integer frame"))
            if not _positive_int(clip.get("duration")):
                issues.append(Issue("invalid_range", f"{cp}.duration", "must be a positive integer frame count"))
            if _frame(clip.get("timeline_start")) and _positive_int(clip.get("duration")):
                spans.append((clip["timeline_start"], clip["timeline_start"] + clip["duration"], str(cid)))
            asset = asset_lookup.get(clip.get("asset_id"))
            if asset and _frame(clip.get("source_in")) and _positive_int(clip.get("duration")) and _positive_int(asset.get("duration_frames")):
                if clip["source_in"] + clip["duration"] > asset["duration_frames"]:
                    issues.append(Issue("invalid_range", cp, "clip source range exceeds the asset duration"))
        spans.sort()
        for previous, current in zip(spans, spans[1:]):
            if current[0] < previous[1]:
                issues.append(Issue("incompatible_overlap", f"{tp}.clips", f"clips {previous[2]!r} and {current[2]!r} overlap"))

    operations = data.get("operations", [])
    if not isinstance(operations, list):
        issues.append(Issue("invalid_type", "$.operations", "must be an array")); operations = []
    operation_ids: set[str] = set()
    for oi, operation in enumerate(operations):
        opath = f"$.operations[{oi}]"
        if not isinstance(operation, dict):
            issues.append(Issue("invalid_type", opath, "operation must be an object")); continue
        kind = operation.get("type")
        if kind not in OP_TYPES:
            issues.append(Issue("unknown_operation", f"{opath}.type", f"unsupported operation: {kind!r}")); continue
        _unknown(operation, COMMON_OP_FIELDS | OP_FIELDS[kind], opath, issues)
        for required in sorted(OP_REQUIRED[kind]):
            if required not in operation:
                issues.append(Issue("required", f"{opath}.{required}", "property is required for this operation"))
        opid = operation.get("id")
        if not isinstance(opid, str) or not opid:
            issues.append(Issue("required", f"{opath}.id", "must be a nonempty stable ID"))
        elif opid in operation_ids:
            issues.append(Issue("duplicate_id", f"{opath}.id", "operation ID is duplicated"))
        else:
            operation_ids.add(opid)
        target = operation.get("target")
        target_optional = kind in {"insert", "reorder", "transition", "audio_mix"}
        if not target_optional and target not in clip_ids:
            issues.append(Issue("missing_target", f"{opath}.target", "must identify a declared clip"))
        for key in ("start", "duration"):
            if key in operation and not (_positive_int(operation[key]) if key == "duration" else _frame(operation[key])):
                issues.append(Issue("invalid_range", f"{opath}.{key}", "must be an integer frame range"))
        if kind == "filter":
            name = operation.get("name")
            props = operation.get("properties", {})
            if name not in FILTERS:
                issues.append(Issue("unsupported_filter", f"{opath}.name", f"filter {name!r} is not in the curated catalog"))
            elif not isinstance(props, dict):
                issues.append(Issue("invalid_filter", f"{opath}.properties", "must be an object"))
            else:
                spec = FILTERS[name]
                for prop in set(props) - set(spec.properties):
                    issues.append(Issue("unsupported_filter_property", f"{opath}.properties.{prop}", "property is not allow-listed"))
                for prop, value in props.items():
                    if prop in spec.properties and (isinstance(value, bool) or not isinstance(value, spec.properties[prop])):
                        issues.append(Issue("invalid_filter_property", f"{opath}.properties.{prop}", "property has the wrong type"))
        if kind == "speed" and (isinstance(operation.get("factor"), bool) or not isinstance(operation.get("factor"), (int, float)) or operation.get("factor", 0) <= 0):
            issues.append(Issue("invalid_speed", f"{opath}.factor", "must be a positive number"))
        if kind == "transition":
            for key in ("from_clip_id", "to_clip_id"):
                if operation.get(key) not in clip_ids:
                    issues.append(Issue("missing_target", f"{opath}.{key}", "must identify a declared clip"))
            if operation.get("kind", "dissolve") not in {"dissolve", "wipe", "fade"}:
                issues.append(Issue("unsupported_transition", f"{opath}.kind", "must be dissolve, wipe, or fade"))
            first, second = operation.get("from_clip_id"), operation.get("to_clip_id")
            if first in clip_tracks and second in clip_tracks and clip_tracks[first] == clip_tracks[second]:
                issues.append(Issue("incompatible_transition", opath, "V1 transitions require clips on different tracks"))
        if kind == "overlay" and operation.get("asset_id") not in asset_ids:
            issues.append(Issue("missing_asset", f"{opath}.asset_id", "must identify a declared asset"))
        if kind == "audio_mix" and target is not None and target not in track_ids:
            issues.append(Issue("missing_target", f"{opath}.target", "must identify a declared track"))
        if kind == "fade_audio" and operation.get("direction") not in {"in", "out"}:
            issues.append(Issue("invalid_fade", f"{opath}.direction", "must be 'in' or 'out'"))
        if kind in {"insert", "reorder"} and operation.get("track_id") not in track_ids:
            issues.append(Issue("missing_target", f"{opath}.track_id", "must identify a declared track"))
        if kind == "insert" and "clip" in operation:
            inserted = operation["clip"]
            if not isinstance(inserted, dict):
                issues.append(Issue("invalid_type", f"{opath}.clip", "must be a clip object"))
            else:
                _unknown(inserted, CLIP_FIELDS, f"{opath}.clip", issues)
                if not isinstance(inserted.get("id"), str) or not inserted.get("id") or inserted.get("id") in clip_ids:
                    issues.append(Issue("duplicate_id", f"{opath}.clip.id", "must be a new nonempty clip ID"))
                if inserted.get("asset_id") not in asset_ids:
                    issues.append(Issue("missing_asset", f"{opath}.clip.asset_id", "must identify a declared asset"))
                if not _frame(inserted.get("timeline_start")) or not _frame(inserted.get("source_in")) or not _positive_int(inserted.get("duration")):
                    issues.append(Issue("invalid_range", f"{opath}.clip", "must have nonnegative start/source frames and positive duration"))
        if kind == "reorder" and "clip_ids" in operation:
            ordered = operation["clip_ids"]
            if not isinstance(ordered, list) or any(not isinstance(value, str) for value in ordered) or len(ordered) != len(set(ordered)):
                issues.append(Issue("invalid_reorder", f"{opath}.clip_ids", "must be an array of unique clip IDs"))
        if kind == "caption" and "text" in operation and not isinstance(operation["text"], str):
            issues.append(Issue("invalid_caption", f"{opath}.text", "must be a string"))
        if kind == "transform" and "keyframes" in operation:
            keyframes = operation["keyframes"]
            if not isinstance(keyframes, list):
                issues.append(Issue("invalid_keyframes", f"{opath}.keyframes", "must be an array"))
            else:
                previous_frame = -1
                for ki, keyframe in enumerate(keyframes):
                    kp = f"{opath}.keyframes[{ki}]"
                    if not isinstance(keyframe, dict) or set(keyframe) - {"frame", "geometry", "opacity"}:
                        issues.append(Issue("invalid_keyframe", kp, "must contain only frame and geometry/opacity")); continue
                    if not _frame(keyframe.get("frame")) or keyframe["frame"] <= previous_frame or not ({"geometry", "opacity"} & set(keyframe)):
                        issues.append(Issue("invalid_keyframe", kp, "frames must increase and include geometry or opacity"))
                    previous_frame = keyframe.get("frame", previous_frame) if isinstance(keyframe.get("frame"), int) else previous_frame
        if kind == "transform" and operation.get("interpolation", "linear") != "linear":
            issues.append(Issue("unsupported_transform_property", f"{opath}.interpolation", "only linear interpolation is supported"))

    export = data.get("export")
    if not isinstance(export, dict):
        issues.append(Issue("required", "$.export", "export must be an object"))
    else:
        _unknown(export, EXPORT_FIELDS, "$.export", issues)
        if export.get("format", "mp4") != "mp4" or export.get("video_codec", "libx264") != "libx264" or export.get("audio_codec", "aac") != "aac":
            issues.append(Issue("unsupported_export", "$.export", "V1 supports MP4 with libx264 video and AAC audio"))

    if not issues:
        from .operations import resolve_timeline

        try:
            resolved_tracks = resolve_timeline(data)
            for track in resolved_tracks:
                spans = sorted((clip["timeline_start"], clip["timeline_start"] + clip["duration"], clip["id"]) for clip in track["clips"])
                for previous, current in zip(spans, spans[1:]):
                    if current[0] < previous[1]:
                        issues.append(Issue("incompatible_overlap", "$.operations", f"resolved clips {previous[2]!r} and {current[2]!r} overlap"))
        except VideoEditingError as exc:
            issues.append(Issue(exc.code, "$.operations", str(exc)))

    if issues:
        raise PlanValidationError(issues)
    return ValidatedPlan(deepcopy(data), source)


def load_plan(path: Path, *, check_files: bool = True) -> ValidatedPlan:
    return validate_plan(read_json(path), source=path, check_files=check_files)
