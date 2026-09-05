"""Strict loading and semantic validation for edit-plan version 1."""

from __future__ import annotations

import json
import math
import re
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any
from urllib.parse import urlsplit

from .errors import Issue, PlanValidationError, VideoEditingError
from .filters import FILTERS
from .probe import fingerprint
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
    resolved_tracks: tuple[dict[str, Any], ...] = ()

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


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _unit_interval(value: Any) -> bool:
    return _number(value) and 0 <= value <= 1


def _safe_relative_path(value: str, base: Path) -> bool:
    path = Path(value)
    if path.is_absolute() or PureWindowsPath(value).is_absolute() or urlsplit(value).scheme:
        return False
    if ".." in path.parts:
        return False
    try:
        (base / path).resolve().relative_to(base.resolve())
    except ValueError:
        return False
    return True


def _color(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"#[0-9a-fA-F]{6}(?:[0-9a-fA-F]{2})?", value) is not None


def _geometry(value: Any) -> bool:
    if not isinstance(value, str) or len(value) > 128:
        return False
    number = r"-?(?:\d+(?:\.\d+)?|\.\d+)"
    return re.fullmatch(fr"{number}%?/{number}%?:{number}%?x{number}%?", value) is not None


def validate_plan(
    data: dict[str, Any],
    *,
    source: Path = Path("edit-plan.json"),
    check_files: bool = True,
    require_confined_paths: bool = False,
) -> ValidatedPlan:
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
    elif len(assets) > 64:
        issues.append(Issue("resource_limit", "$.assets", "must contain at most 64 assets"))
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
        else:
            if require_confined_paths and not _safe_relative_path(path_value, base):
                issues.append(Issue("unsafe_path", f"{p}.path", "must be a job-relative path without traversal, URLs, or drive prefixes"))
            asset_path = (base / path_value).resolve()
            if check_files and not asset_path.is_file():
                issues.append(Issue("missing_asset", f"{p}.path", f"asset does not exist: {path_value}"))
        if asset.get("kind") not in {"video", "audio", "image"}:
            issues.append(Issue("invalid_asset", f"{p}.kind", "must be video, audio, or image"))
        if not _positive_int(asset.get("duration_frames")):
            issues.append(Issue("invalid_asset", f"{p}.duration_frames", "must be a positive integer"))
        declared_fingerprint = asset.get("fingerprint")
        if not isinstance(declared_fingerprint, str) or re.fullmatch(r"sha256:[0-9a-f]{64}", declared_fingerprint) is None:
            issues.append(Issue("invalid_fingerprint", f"{p}.fingerprint", "must be a lowercase SHA-256 fingerprint"))
        elif check_files and isinstance(path_value, str) and (base / path_value).resolve().is_file():
            if fingerprint((base / path_value).resolve()) != declared_fingerprint:
                issues.append(Issue("fingerprint_mismatch", f"{p}.fingerprint", "does not match the current asset bytes"))

    tracks = data.get("tracks")
    if not isinstance(tracks, list) or not tracks:
        issues.append(Issue("required", "$.tracks", "tracks must be a nonempty array"))
        tracks = []
    elif len(tracks) > 32:
        issues.append(Issue("resource_limit", "$.tracks", "must contain at most 32 tracks"))
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
        if len(clips) > 512:
            issues.append(Issue("resource_limit", f"{tp}.clips", "must contain at most 512 clips"))
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
            if asset and track.get("kind") == "video" and asset.get("kind") == "audio":
                issues.append(Issue("incompatible_media_kind", f"{cp}.asset_id", "audio-only assets cannot be placed on video tracks"))
            if asset and track.get("kind") == "audio" and asset.get("kind") == "image":
                issues.append(Issue("incompatible_media_kind", f"{cp}.asset_id", "image assets cannot be placed on audio tracks"))
        spans.sort()
        for previous, current in zip(spans, spans[1:]):
            if current[0] < previous[1]:
                issues.append(Issue("incompatible_overlap", f"{tp}.clips", f"clips {previous[2]!r} and {current[2]!r} overlap"))

    operations = data.get("operations", [])
    if not isinstance(operations, list):
        issues.append(Issue("invalid_type", "$.operations", "must be an array")); operations = []
    elif len(operations) > 1024:
        issues.append(Issue("resource_limit", "$.operations", "must contain at most 1024 operations"))
    operation_ids: set[str] = set()
    prospective_clip_ids = set(clip_ids)
    for operation in operations:
        if not isinstance(operation, dict):
            continue
        if operation.get("type") == "insert" and isinstance(operation.get("clip"), dict):
            inserted_id = operation["clip"].get("id")
            if isinstance(inserted_id, str):
                prospective_clip_ids.add(inserted_id)
        if operation.get("type") == "split" and isinstance(operation.get("target"), str) and isinstance(operation.get("id"), str):
            prospective_clip_ids.add(f"{operation['target']}__{operation['id']}")
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
        if not target_optional and target not in prospective_clip_ids:
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
                if name == "brightness" and ("level" not in props or (_number(props.get("level")) and props["level"] < -1)):
                    issues.append(Issue("invalid_filter_property", f"{opath}.properties.level", "brightness requires a level of at least -1"))
                for prop in set(props) - set(spec.properties):
                    issues.append(Issue("unsupported_filter_property", f"{opath}.properties.{prop}", "property is not allow-listed"))
                for prop, value in props.items():
                    if prop in spec.properties and (isinstance(value, bool) or not isinstance(value, spec.properties[prop])):
                        issues.append(Issue("invalid_filter_property", f"{opath}.properties.{prop}", "property has the wrong type"))
                    elif prop in spec.properties and not _number(value):
                        issues.append(Issue("invalid_filter_property", f"{opath}.properties.{prop}", "property must be finite"))
        if kind == "speed" and (not _number(operation.get("factor")) or operation.get("factor", 0) <= 0):
            issues.append(Issue("invalid_speed", f"{opath}.factor", "must be a positive number"))
        if kind == "transition":
            for key in ("from_clip_id", "to_clip_id"):
                if operation.get(key) not in prospective_clip_ids:
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
                inserted_asset = asset_lookup.get(inserted.get("asset_id"))
                if inserted_asset and _frame(inserted.get("source_in")) and _positive_int(inserted.get("duration")):
                    if inserted["source_in"] + inserted["duration"] > inserted_asset.get("duration_frames", 0):
                        issues.append(Issue("invalid_range", f"{opath}.clip", "inserted clip source range exceeds the asset duration"))
        if kind == "reorder" and "clip_ids" in operation:
            ordered = operation["clip_ids"]
            if not isinstance(ordered, list) or any(not isinstance(value, str) for value in ordered) or len(ordered) != len(set(ordered)):
                issues.append(Issue("invalid_reorder", f"{opath}.clip_ids", "must be an array of unique clip IDs"))
        if kind == "caption" and "text" in operation and not isinstance(operation["text"], str):
            issues.append(Issue("invalid_caption", f"{opath}.text", "must be a string"))
        if kind == "caption" and isinstance(operation.get("text"), str) and len(operation["text"]) > 4096:
            issues.append(Issue("invalid_caption", f"{opath}.text", "must not exceed 4096 characters"))
        if kind == "caption":
            if "font" in operation and (not isinstance(operation["font"], str) or not operation["font"] or len(operation["font"]) > 256):
                issues.append(Issue("invalid_caption", f"{opath}.font", "must be a nonempty string of at most 256 characters"))
            if "size" in operation and not _positive_int(operation["size"]):
                issues.append(Issue("invalid_caption", f"{opath}.size", "must be a positive integer"))
            if operation.get("halign", "center") not in {"left", "center", "right"}:
                issues.append(Issue("invalid_caption", f"{opath}.halign", "must be left, center, or right"))
            if operation.get("valign", "middle") not in {"top", "middle", "bottom"}:
                issues.append(Issue("invalid_caption", f"{opath}.valign", "must be top, middle, or bottom"))
        for field in ("geometry",):
            if field in operation and not _geometry(operation[field]):
                issues.append(Issue("invalid_geometry", f"{opath}.{field}", "must use x/y:widthxheight numeric or percent geometry"))
        for field in ("color", "background"):
            if field in operation and not _color(operation[field]):
                issues.append(Issue("invalid_color", f"{opath}.{field}", "must be #RRGGBB or #RRGGBBAA"))
        if "opacity" in operation and not _unit_interval(operation["opacity"]):
            issues.append(Issue("invalid_opacity", f"{opath}.opacity", "must be a number from 0 through 1"))
        if kind in {"chroma_key", "mask"}:
            field = "variance" if kind == "chroma_key" else "softness"
            if field in operation and not _unit_interval(operation[field]):
                issues.append(Issue("invalid_range", f"{opath}.{field}", "must be a number from 0 through 1"))
        if kind in {"volume", "audio_mix"} and "gain_db" in operation and not _number(operation["gain_db"]):
            issues.append(Issue("invalid_audio_value", f"{opath}.gain_db", "must be a number"))
        if kind == "volume" and "level" in operation:
            value = operation["level"]
            if not _number(value) and (not isinstance(value, str) or re.fullmatch(r"-?\d+(?:\.\d+)?dB", value) is None):
                issues.append(Issue("invalid_audio_value", f"{opath}.level", "must be a finite number or a dB value such as -6dB"))
        if kind == "transform" and "rotation" in operation and not _number(operation["rotation"]):
            issues.append(Issue("invalid_transform", f"{opath}.rotation", "must be a finite number"))
        if kind == "mask":
            if not isinstance(operation.get("resource"), str) or not operation.get("resource"):
                issues.append(Issue("invalid_mask", f"{opath}.resource", "must be a nonempty string"))
            if "invert" in operation and not isinstance(operation["invert"], bool):
                issues.append(Issue("invalid_mask", f"{opath}.invert", "must be a boolean"))
        if kind == "fade_audio":
            for field in ("from", "to"):
                if field in operation and not _unit_interval(operation[field]):
                    issues.append(Issue("invalid_audio_value", f"{opath}.{field}", "must be a number from 0 through 1"))
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
                    if "geometry" in keyframe and not _geometry(keyframe["geometry"]):
                        issues.append(Issue("invalid_geometry", f"{kp}.geometry", "must use x/y:widthxheight numeric or percent geometry"))
                    if "opacity" in keyframe and not _unit_interval(keyframe["opacity"]):
                        issues.append(Issue("invalid_opacity", f"{kp}.opacity", "must be a number from 0 through 1"))
                    previous_frame = keyframe.get("frame", previous_frame) if isinstance(keyframe.get("frame"), int) else previous_frame
        if kind == "transform" and operation.get("interpolation", "linear") != "linear":
            issues.append(Issue("unsupported_transform_property", f"{opath}.interpolation", "only linear interpolation is supported"))
        if kind == "volume" and "level" in operation and "gain_db" in operation:
            issues.append(Issue("conflicting_operation_property", opath, "volume must use level or gain_db, not both"))

    export = data.get("export")
    if not isinstance(export, dict):
        issues.append(Issue("required", "$.export", "export must be an object"))
    else:
        _unknown(export, EXPORT_FIELDS, "$.export", issues)
        if export.get("format", "mp4") != "mp4" or export.get("video_codec", "libx264") != "libx264" or export.get("audio_codec", "aac") != "aac":
            issues.append(Issue("unsupported_export", "$.export", "V1 supports MP4 with libx264 video and AAC audio"))
        for field in ("video_bitrate", "audio_bitrate"):
            if field in export and (not isinstance(export[field], str) or re.fullmatch(r"[1-9]\d*(?:[kKmM])?", export[field]) is None):
                issues.append(Issue("invalid_export", f"$.export.{field}", "must be a positive bitrate such as 192k or 12M"))
        if export.get("pixel_format", "yuv420p") != "yuv420p":
            issues.append(Issue("unsupported_export", "$.export.pixel_format", "V1 supports only yuv420p"))
        if export.get("movflags", "+faststart") != "+faststart":
            issues.append(Issue("unsupported_export", "$.export.movflags", "V1 supports only +faststart"))

    resolved_tracks: list[dict[str, Any]] = []
    if not issues:
        from .operations import resolve_timeline

        try:
            resolved_tracks = resolve_timeline(data)
            resolved_clip_ids = {clip["id"] for track in resolved_tracks for clip in track["clips"]}
            resolved_clips = {clip["id"]: clip for track in resolved_tracks for clip in track["clips"]}
            active_clip_ids = {clip_id for clip_id, clip in resolved_clips.items() if clip.get("enabled", True)}
            for track in resolved_tracks:
                spans = sorted((clip["timeline_start"], clip["timeline_start"] + clip["duration"], clip["id"]) for clip in track["clips"])
                for previous, current in zip(spans, spans[1:]):
                    if current[0] < previous[1]:
                        issues.append(Issue("incompatible_overlap", "$.operations", f"resolved clips {previous[2]!r} and {current[2]!r} overlap"))
                for clip in track["clips"]:
                    asset = asset_lookup[clip["asset_id"]]
                    if clip["source_in"] + clip["duration"] > asset["duration_frames"]:
                        issues.append(Issue("invalid_range", "$.operations", f"resolved clip {clip['id']!r} exceeds asset duration"))
            timed_effects = {"caption", "overlay", "transform", "volume", "fade_audio", "chroma_key", "mask", "filter"}
            speed_targets: set[str] = set()
            transform_spans: dict[str, list[tuple[int, int]]] = {}
            if not active_clip_ids:
                issues.append(Issue("empty_timeline", "$.operations", "structural operations leave no enabled clips to render"))
            for index, operation in enumerate(operations):
                if not operation.get("enabled", True):
                    continue
                kind = operation["type"]
                target = operation.get("target")
                if kind not in {"insert", "reorder", "transition", "audio_mix"} and target not in active_clip_ids:
                    issues.append(Issue("missing_target", f"$.operations[{index}].target", "target is absent or disabled after structural operations"))
                    continue
                if kind in timed_effects and target in resolved_clips:
                    clip = resolved_clips[target]
                    start = operation.get("start", 0)
                    duration = operation.get("duration", clip["duration"] - start)
                    if start >= clip["duration"] or duration <= 0 or start + duration > clip["duration"]:
                        issues.append(Issue("invalid_range", f"$.operations[{index}]", "operation range must be within its target clip"))
                    if kind == "transform":
                        # Geometry transforms compose in MLT, making an accidental
                        # overlap a double zoom. Opacity-only fades may overlap geometry.
                        if "geometry" in operation or any("geometry" in key for key in operation.get("keyframes", [])):
                            spans = transform_spans.setdefault(target, [])
                            if any(start < end and begin < start + duration for begin, end in spans):
                                issues.append(Issue("conflicting_transform", f"$.operations[{index}]", "geometry transform intervals on the same clip must not overlap"))
                            spans.append((start, start + duration))
                        for key_index, keyframe in enumerate(operation.get("keyframes", [])):
                            if keyframe["frame"] >= duration:
                                issues.append(Issue("invalid_keyframe", f"$.operations[{index}].keyframes[{key_index}].frame", "must be within the transform duration"))
                if kind == "speed" and isinstance(target, str):
                    if target in speed_targets:
                        issues.append(Issue("conflicting_operation", f"$.operations[{index}]", "only one speed operation is supported per clip"))
                    speed_targets.add(target)
                if kind == "transition":
                    for field in ("from_clip_id", "to_clip_id"):
                        if operation.get(field) not in active_clip_ids:
                            issues.append(Issue("missing_target", f"$.operations[{index}].{field}", "clip is absent or disabled after structural operations"))
        except VideoEditingError as exc:
            issues.append(Issue(exc.code, "$.operations", str(exc)))

    if issues:
        raise PlanValidationError(issues)
    return ValidatedPlan(deepcopy(data), source, tuple(deepcopy(resolved_tracks)))


def load_plan(path: Path, *, check_files: bool = True, require_confined_paths: bool = False) -> ValidatedPlan:
    return validate_plan(read_json(path), source=path, check_files=check_files, require_confined_paths=require_confined_paths)
