"""Shotcut-compatible MLT XML compilation using ElementTree APIs."""

from __future__ import annotations

import os
import math
import xml.etree.ElementTree as ET
from copy import deepcopy
from pathlib import Path
from typing import Any

from .errors import VideoEditingError
from .filters import FILTERS, FILTER_CATALOG_VERSION
from .operations import resolve_timeline
from .plan import ValidatedPlan
from .timebase import frames_to_mlt_time


GENERATOR = "video-editing-skill/0.1.0"
TRANSFORM_FIELDS = {
    "id", "type", "target", "start", "duration", "enabled",
    "geometry", "rotation", "opacity", "keyframes", "interpolation",
}


def _property(parent: ET.Element, name: str, value: Any) -> ET.Element:
    child = ET.SubElement(parent, "property", {"name": name})
    child.text = str(value)
    return child


def _transform_property(parent: ET.Element, name: str, value: Any) -> ET.Element:
    if parent.find(f"./property[@name='{name}']") is not None:
        raise VideoEditingError(f"duplicate transform property: {name}", code="duplicate_transform_property")
    return _property(parent, name, value)


def _mlt_color(value: str) -> str:
    """Convert plan CSS-style #RRGGBBAA colors to MLT #AARRGGBB."""
    if len(value) == 9 and value.startswith("#"):
        return f"#{value[7:9]}{value[1:7]}"
    return value


def _resource_path(asset_path: Path, output: Path) -> str:
    try:
        relative = asset_path.resolve().relative_to(output.resolve().parent)
        return relative.as_posix()
    except ValueError:
        return str(asset_path.resolve())


def _time(frame: int, profile: dict[str, Any]) -> str:
    rate = profile["frame_rate"]
    return frames_to_mlt_time(frame, rate["numerator"], rate["denominator"])


def _new_root(plan: dict[str, Any], output: Path) -> ET.Element:
    profile = plan["profile"]
    rate = profile["frame_rate"]
    root = ET.Element("mlt", {
        "LC_NUMERIC": "C", "version": "7.28.0", "title": output.stem,
        "producer": "ves_main",
    })
    ET.SubElement(root, "profile", {
        "description": "Video Editing Skill profile",
        "width": str(profile["width"]), "height": str(profile["height"]),
        "progressive": "1" if profile.get("progressive", True) else "0",
        "sample_aspect_num": "1", "sample_aspect_den": "1",
        "display_aspect_num": str(profile["width"]), "display_aspect_den": str(profile["height"]),
        "frame_rate_num": str(rate["numerator"]), "frame_rate_den": str(rate["denominator"]),
        "colorspace": str(profile.get("colorspace", 709)),
    })
    return root


def _load_base(base_project: Path, plan: dict[str, Any]) -> ET.Element:
    try:
        tree = ET.parse(base_project)
    except (OSError, ET.ParseError) as exc:
        raise VideoEditingError(f"cannot parse base MLT project: {exc}", code="invalid_base_project") from exc
    root = tree.getroot()
    if root.tag != "mlt" or root.find("profile") is None:
        raise VideoEditingError("base project must have an MLT root and profile", code="unsupported_base_project")
    profile = root.find("profile")
    assert profile is not None
    expected = plan["profile"]
    rate = expected["frame_rate"]
    comparisons = {
        "width": expected["width"], "height": expected["height"],
        "frame_rate_num": rate["numerator"], "frame_rate_den": rate["denominator"],
    }
    for key, value in comparisons.items():
        if profile.get(key) != str(value):
            raise VideoEditingError(f"base project profile mismatch for {key}", code="profile_mismatch")
    existing_ids = {element.get("id") for element in root.iter() if element.get("id")}
    if any(value and value.startswith("ves_") for value in existing_ids):
        raise VideoEditingError("base project contains IDs reserved by video-editing-skill", code="id_collision")
    for operation in plan.get("operations", []):
        target = operation.get("target")
        if isinstance(target, str) and target.startswith("base:"):
            raise VideoEditingError(
                f"operation {operation['id']} intersects a base-project structure not owned by this tool",
                code="unsupported_base_intersection",
            )
    return deepcopy(root)


def _add_filter(producer: ET.Element, operation: dict[str, Any], profile: dict[str, Any]) -> None:
    kind = operation["type"]
    start = operation.get("start", 0)
    duration = operation.get("duration")
    attrs = {"id": f"ves_filter_{operation['id']}", "in": _time(start, profile)}
    if duration:
        attrs["out"] = _time(start + duration - 1, profile)
    node = ET.SubElement(producer, "filter", attrs)
    if kind == "filter":
        spec = FILTERS[operation["name"]]
        _property(node, "mlt_service", spec.service)
        if operation["name"] == "brightness":
            _property(node, "shotcut:filter", "brightness")
        for name, value in spec.transform(operation.get("properties", {})).items():
            _property(node, name, value)
    elif kind == "transform":
        unsupported = sorted(set(operation) - TRANSFORM_FIELDS)
        if unsupported:
            raise VideoEditingError(
                f"unsupported transform properties: {', '.join(unsupported)}",
                code="unsupported_transform_property",
            )
        if operation.get("interpolation", "linear") != "linear":
            raise VideoEditingError("only linear transform interpolation is supported", code="unsupported_transform_property")
        keyframes = operation.get("keyframes", [])
        for index, item in enumerate(keyframes):
            if not isinstance(item, dict) or set(item) - {"frame", "geometry", "opacity"}:
                raise VideoEditingError(
                    f"unsupported transform keyframe properties at index {index}",
                    code="unsupported_transform_property",
                )
        _transform_property(node, "mlt_service", "affine")
        _transform_property(node, "shotcut:filter", "affineSizePosition")
        _transform_property(node, "transition.fix_rotate_x", operation.get("rotation", 0))
        _transform_property(node, "transition.threads", 0)
        geometry_keys = [item for item in keyframes if "geometry" in item]
        opacity_keys = [item for item in keyframes if "opacity" in item]
        if geometry_keys:
            _transform_property(node, "transition.rect", ";".join(f"{item['frame']}={item['geometry']}" for item in geometry_keys))
        else:
            _transform_property(node, "transition.rect", operation.get("geometry", "0%/0%:100%x100%"))
        if opacity_keys:
            _transform_property(node, "transition.mix", ";".join(f"{item['frame']}={item['opacity']}" for item in opacity_keys))
        else:
            _transform_property(node, "transition.mix", operation.get("opacity", 1))
    elif kind == "volume":
        _property(node, "mlt_service", "volume")
        _property(node, "level", operation.get("level", f"{operation.get('gain_db', 0)}dB"))
    elif kind == "fade_audio":
        _property(node, "mlt_service", "volume")
        from_value = operation.get("from", 0 if operation.get("direction") == "in" else 1)
        to_value = operation.get("to", 1 if operation.get("direction") == "in" else 0)
        # Plan endpoints are linear amplitudes; MLT's animated level is in dB.
        def decibels(amplitude: float) -> str:
            return f"{20 * math.log10(amplitude):.6g}" if amplitude > 0 else "-90"
        _property(node, "shotcut:filter", "fadeInVolume" if operation["direction"] == "in" else "fadeOutVolume")
        _property(node, "level", f"0={decibels(from_value)};{operation['duration'] - 1}={decibels(to_value)}")
    elif kind == "chroma_key":
        _property(node, "mlt_service", "frei0r.bluescreen0r")
        _property(node, "Color", operation.get("color", "#00ff00"))
        _property(node, "Distance", operation.get("variance", 0.25))
    elif kind == "mask":
        _property(node, "mlt_service", "shape")
        _property(node, "resource", operation["resource"])
        _property(node, "mix", operation.get("softness", 0))
        _property(node, "invert", int(bool(operation.get("invert", False))))


def compile_mlt(validated: ValidatedPlan, output: Path, *, base_project: Path | None = None) -> ET.ElementTree:
    plan = validated.data
    root = _load_base(base_project, plan) if base_project else _new_root(plan, output)
    profile = plan["profile"]
    base_dir = validated.source.resolve().parent
    assets = {asset["id"]: asset for asset in plan["assets"]}
    tracks = deepcopy(list(validated.resolved_tracks)) if validated.resolved_tracks else resolve_timeline(plan)
    # Track 0 is an explicit black compositing base. Shotcut previews a tractor
    # without one as transparent even though file consumers may flatten it.
    track_indexes = {track["id"]: index + 1 for index, track in enumerate(tracks)}
    clip_track_indexes = {clip["id"]: index + 1 for index, track in enumerate(tracks) for clip in track["clips"]}
    effects_by_target: dict[str, list[dict[str, Any]]] = {}
    speed_by_target: dict[str, dict[str, Any]] = {}
    for operation in plan.get("operations", []):
        if operation.get("enabled", True) and operation["type"] in {"transform", "volume", "fade_audio", "chroma_key", "mask", "filter"}:
            effects_by_target.setdefault(operation["target"], []).append(operation)
        if operation.get("enabled", True) and operation["type"] == "speed":
            speed_by_target[operation["target"]] = operation

    timeline_frames = max((
        clip["timeline_start"] + clip["duration"]
        for track in tracks for clip in track["clips"] if clip.get("enabled", True)
    ), default=1)
    clip_lookup = {clip["id"]: clip for track in tracks for clip in track["clips"]}
    for operation in plan.get("operations", []):
        if operation.get("enabled", True) and operation["type"] in {"caption", "overlay"}:
            target_clip = clip_lookup.get(operation.get("target"))
            start = operation.get("start", 0) + (target_clip["timeline_start"] if target_clip else 0)
            timeline_frames = max(timeline_frames, start + operation["duration"])

    background = ET.SubElement(root, "producer", {
        "id": "ves_background_producer", "in": _time(0, profile),
        "out": _time(timeline_frames - 1, profile),
    })
    _property(background, "resource", "black")
    _property(background, "mlt_service", "color")
    _property(background, "length", _time(timeline_frames, profile))
    background_playlist = ET.SubElement(root, "playlist", {"id": "ves_background"})
    ET.SubElement(background_playlist, "entry", {
        "producer": "ves_background_producer", "in": _time(0, profile),
        "out": _time(timeline_frames - 1, profile),
    })

    playlist_ids: list[str] = ["ves_background"]
    for track_index, track in enumerate(tracks):
        playlist_id = f"ves_playlist_{track_index}_{track['id']}"
        playlist_ids.append(playlist_id)
        playlist = ET.SubElement(root, "playlist", {"id": playlist_id})
        _property(playlist, "shotcut:video", 1 if track["kind"] == "video" else 0)
        _property(playlist, "shotcut:audio", 1 if track["kind"] == "audio" else 0)
        _property(playlist, "shotcut:name", track.get("name", track["id"]))
        cursor = 0
        for clip_index, clip in enumerate(track["clips"]):
            if not clip.get("enabled", True):
                continue
            if clip["timeline_start"] > cursor:
                ET.SubElement(playlist, "blank", {"length": _time(clip["timeline_start"] - cursor, profile)})
            asset = assets[clip["asset_id"]]
            producer_id = f"ves_producer_{track_index}_{clip_index}_{clip['id']}"
            producer = ET.SubElement(root, "producer", {
                "id": producer_id,
                "in": _time(0, profile),
                "out": _time(asset["duration_frames"] - 1, profile),
            })
            asset_path = (base_dir / asset["path"]).resolve()
            resource = _resource_path(asset_path, output)
            speed = speed_by_target.get(clip["id"])
            if speed:
                _property(producer, "resource", f"{speed['factor']}:{resource}")
                _property(producer, "mlt_service", "timewarp")
                _property(producer, "warp_speed", speed["factor"])
                _property(producer, "warp_resource", resource)
            else:
                _property(producer, "resource", resource)
                _property(producer, "mlt_service", "qimage" if asset["kind"] == "image" else "avformat-novalidate")
            _property(producer, "length", _time(asset["duration_frames"], profile))
            _property(producer, "shotcut:caption", clip["id"])
            _property(producer, "video-editing-skill:asset-id", asset["id"])
            for operation in effects_by_target.get(clip["id"], []):
                _add_filter(producer, operation, profile)
            entry = ET.SubElement(playlist, "entry", {
                "producer": producer_id,
                "in": _time(clip["source_in"], profile),
                "out": _time(clip["source_in"] + clip["duration"] - 1, profile),
            })
            if track.get("muted"):
                _property(entry, "hide", "audio")
            cursor = clip["timeline_start"] + clip["duration"]

    # Captions and overlays are modeled as explicit overlay tracks.
    for operation in plan.get("operations", []):
        if not operation.get("enabled", True) or operation["type"] not in {"caption", "overlay"}:
            continue
        playlist_id = f"ves_overlay_{operation['id']}"
        producer_id = f"ves_overlay_producer_{operation['id']}"
        playlist_ids.append(playlist_id)
        playlist = ET.SubElement(root, "playlist", {"id": playlist_id})
        target_clip = clip_lookup.get(operation.get("target"))
        start = operation.get("start", 0) + (target_clip["timeline_start"] if target_clip else 0)
        duration = operation["duration"]
        if start:
            ET.SubElement(playlist, "blank", {"length": _time(start, profile)})
        producer = ET.SubElement(root, "producer", {"id": producer_id, "in": _time(0, profile), "out": _time(duration - 1, profile)})
        if operation["type"] == "caption":
            _property(producer, "mlt_service", "color")
            _property(producer, "resource", "0x00000000")
            _property(producer, "length", _time(duration, profile))
            filter_node = ET.SubElement(producer, "filter", {"id": f"ves_caption_filter_{operation['id']}"})
            _property(filter_node, "mlt_service", "dynamictext")
            _property(filter_node, "argument", operation["text"])
            _property(filter_node, "geometry", operation.get("geometry", "5%/80%:90%x15%"))
            _property(filter_node, "family", operation.get("font", "Sans"))
            _property(filter_node, "size", operation.get("size", 48))
            _property(filter_node, "fgcolour", _mlt_color(operation.get("color", "#ffffffff")))
            _property(filter_node, "bgcolour", _mlt_color(operation.get("background", "#00000080")))
            _property(filter_node, "halign", operation.get("halign", "center"))
            _property(filter_node, "valign", operation.get("valign", "middle"))
        else:
            asset = assets[operation["asset_id"]]
            _property(producer, "mlt_service", "qimage" if asset["kind"] == "image" else "avformat-novalidate")
            _property(producer, "resource", _resource_path((base_dir / asset["path"]).resolve(), output))
            filter_node = ET.SubElement(producer, "filter", {"id": f"ves_overlay_filter_{operation['id']}"})
            _property(filter_node, "mlt_service", "affine")
            _property(filter_node, "transition.rect", operation.get("geometry", "0%/0%:100%x100%"))
            _property(filter_node, "transition.mix", operation.get("opacity", 1))
        ET.SubElement(playlist, "entry", {"producer": producer_id, "in": _time(0, profile), "out": _time(duration - 1, profile)})

    tractor = ET.SubElement(root, "tractor", {"id": "ves_main", "title": "Shotcut-compatible edit"})
    root.set("producer", "ves_main")
    _property(tractor, "shotcut", "1")
    _property(tractor, "shotcut:projectAudioChannels", profile["channels"])
    _property(tractor, "video-editing-skill:profile.sample_rate", profile["sample_rate"])
    _property(tractor, "video-editing-skill:profile.channels", profile["channels"])
    _property(tractor, "shotcut:projectFolder", "0")
    _property(tractor, "video-editing-skill:generator", GENERATOR)
    _property(tractor, "video-editing-skill:filter-catalog", FILTER_CATALOG_VERSION)
    for name, value in sorted(plan["export"].items()):
        _property(tractor, f"video-editing-skill:export.{name}", value)
    multitrack = ET.SubElement(tractor, "multitrack")
    playlist_kinds = ["background", *(track["kind"] for track in tracks)]
    playlist_kinds.extend("video" for _ in range(len(playlist_ids) - len(playlist_kinds)))
    for index, playlist_id in enumerate(playlist_ids):
        attributes = {"producer": playlist_id}
        if 0 < index <= len(tracks):
            track = tracks[index - 1]
            # Video clips retain their embedded audio unless explicitly muted.
            hide_audio = bool(track.get("muted"))
            hide_video = track["kind"] == "audio" or bool(track.get("hidden"))
            if hide_audio and hide_video:
                attributes["hide"] = "both"
            elif hide_audio:
                attributes["hide"] = "audio"
            elif hide_video:
                attributes["hide"] = "video"
        ET.SubElement(multitrack, "track", attributes)
    video_track_indexes = [index for index, kind in enumerate(playlist_kinds) if kind == "video"]
    for index in video_track_indexes:
        transition = ET.SubElement(tractor, "transition", {"id": f"ves_composite_{index}"})
        _property(transition, "a_track", 0)
        _property(transition, "b_track", index)
        _property(transition, "mlt_service", "qtblend")
        _property(transition, "always_active", 1)
    audio_track_indexes = [index + 1 for index, track in enumerate(tracks) if not track.get("muted", False)]
    for index in audio_track_indexes:
        transition = ET.SubElement(tractor, "transition", {"id": f"ves_audio_mix_track_{index}"})
        _property(transition, "a_track", 0)
        _property(transition, "b_track", index)
        _property(transition, "mlt_service", "mix")
        _property(transition, "always_active", 1)
    for operation in plan.get("operations", []):
        if operation.get("enabled", True) and operation["type"] == "transition":
            transition = ET.SubElement(tractor, "transition", {
                "id": f"ves_transition_{operation['id']}",
                "in": _time(operation.get("start", 0), profile),
                "out": _time(operation.get("start", 0) + operation["duration"] - 1, profile),
            })
            _property(transition, "mlt_service", "luma")
            _property(transition, "resource", operation.get("kind", "dissolve"))
            _property(transition, "a_track", clip_track_indexes[operation["from_clip_id"]])
            _property(transition, "b_track", clip_track_indexes[operation["to_clip_id"]])
        if operation.get("enabled", True) and operation["type"] == "audio_mix":
            transition = ET.SubElement(tractor, "transition", {"id": f"ves_audio_mix_{operation['id']}"})
            _property(transition, "mlt_service", "mix")
            _property(transition, "a_track", 0)
            target = operation.get("target")
            _property(transition, "b_track", track_indexes.get(target, 1))
            _property(transition, "start", operation.get("gain_db", "0dB"))
            _property(transition, "always_active", 1)

    # MLT's XML loader resolves references in document order. Keep all
    # generated producers before their playlists, while leaving pre-existing
    # base-project nodes in their original relative order.
    generated = [child for child in root if (child.get("id") or "").startswith("ves_")]
    for child in generated:
        root.remove(child)
    for tag in ("producer", "playlist", "tractor"):
        for child in generated:
            if child.tag == tag:
                root.append(child)
    return ET.ElementTree(root)


def write_mlt(validated: ValidatedPlan, output: Path, *, base_project: Path | None = None) -> None:
    if output.exists():
        raise VideoEditingError(f"refusing to overwrite existing project: {output}", code="output_exists")
    if base_project and output.resolve() == base_project.resolve():
        raise VideoEditingError("output must differ from the base project", code="unsafe_output")
    output.parent.mkdir(parents=True, exist_ok=True)
    tree = compile_mlt(validated, output, base_project=base_project)
    ET.indent(tree, space="  ")
    temporary = output.with_name(f".{output.name}.tmp")
    try:
        tree.write(temporary, encoding="utf-8", xml_declaration=True, short_empty_elements=True)
        os.replace(temporary, output)
    finally:
        if temporary.exists():
            temporary.unlink()
