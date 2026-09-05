"""Compilation and rendered-output gates plus immutable result manifests."""

from __future__ import annotations

import hashlib
import json
import re
import xml.etree.ElementTree as ET
from fractions import Fraction
from pathlib import Path
from typing import Any

from .errors import ExternalToolError, VideoEditingError
from .operations import resolve_timeline
from .plan import ValidatedPlan
from .supervisor import ProcessSupervisor, Toolchain


BLACK_SEGMENT_RE = re.compile(r"black_start:(?P<start>[0-9.]+)\s+black_end:(?P<end>[0-9.]+)\s+black_duration:(?P<duration>[0-9.]+)")


def sha256(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(chunk_size):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def artifact_record(path: Path, root: Path) -> dict[str, Any]:
    resolved = path.resolve()
    try:
        relative = resolved.relative_to(root.resolve()).as_posix()
    except ValueError as exc:
        raise VideoEditingError(f"artifact is outside the job workspace: {path}", code="unsafe_path") from exc
    return {"path": relative, "size_bytes": resolved.stat().st_size, "fingerprint": sha256(resolved)}


def validate_compiled_mlt(project: Path, plan: ValidatedPlan, workspace_root: Path) -> dict[str, Any]:
    """Reject dangling references, escaped resources, and missing compiler effects."""
    try:
        root = ET.parse(project).getroot()
    except (OSError, ET.ParseError) as exc:
        raise VideoEditingError(f"compiled MLT is not valid XML: {exc}", code="invalid_compiled_mlt") from exc
    if root.tag != "mlt" or root.get("producer") != "ves_main" or root.get("root") is not None:
        raise VideoEditingError("compiled MLT has an unexpected root or producer", code="invalid_compiled_mlt")
    identifiers = {element.get("id") for element in root.iter() if element.get("id")}
    references: list[str] = []
    for element in root.iter():
        if element.tag in {"entry", "track"} and element.get("producer"):
            references.append(str(element.get("producer")))
    missing = sorted(set(references) - identifiers)
    if missing:
        raise VideoEditingError(f"compiled MLT has dangling producer references: {', '.join(missing)}", code="invalid_compiled_mlt")

    allowed = {
        (plan.source.resolve().parent / asset["path"]).resolve()
        for asset in plan.data["assets"]
    }
    root_dir = workspace_root.resolve()
    resources: list[str] = []
    for producer in root.findall("producer"):
        service = producer.find("./property[@name='mlt_service']")
        resource = producer.find("./property[@name='resource']")
        if resource is None or resource.text is None or (service is not None and service.text == "color"):
            continue
        value = resource.text
        if service is not None and service.text == "timewarp":
            value = value.split(":", 1)[-1]
        candidate = Path(value)
        if candidate.is_absolute() or re.match(r"^[A-Za-z]:[\\/]", value) or "://" in value:
            raise VideoEditingError(f"compiled MLT contains a nonlocal resource: {resource.text}", code="unsafe_mlt_resource")
        resolved = (project.resolve().parent / candidate).resolve()
        try:
            resolved.relative_to(root_dir)
        except ValueError as exc:
            raise VideoEditingError(f"compiled MLT resource escapes the job: {resource.text}", code="unsafe_mlt_resource") from exc
        if resolved not in allowed or not resolved.is_file():
            raise VideoEditingError(f"compiled MLT references an undeclared resource: {resource.text}", code="unsafe_mlt_resource")
        resources.append(value)

    for operation in plan.data.get("operations", []):
        if not operation.get("enabled", True) or operation.get("type") != "transform":
            continue
        node = root.find(f"./producer/filter[@id='ves_filter_{operation['id']}']")
        if node is None or node.find("./property[@name='transition.rect']") is None:
            raise VideoEditingError(f"compiled transform is missing: {operation['id']}", code="compiled_effect_missing")
    from .mlt import compile_mlt

    expected = compile_mlt(plan, project).getroot()
    # Ignore formatting whitespace, but verify every generated value (including
    # rational frame rate, brightness, audio routing and transform intervals).
    def canonical(element):
        return (element.tag, sorted(element.attrib.items()), (element.text or "").strip(),
                tuple(canonical(child) for child in element))
    if canonical(root) != canonical(expected):
        raise VideoEditingError("compiled MLT does not match the validated plan", code="invalid_compiled_mlt")
    return {
        "status": "passed",
        "ids": len(identifiers),
        "references": len(references),
        "resources": sorted(resources),
        "project": artifact_record(project, workspace_root),
    }


def expected_output_frames(plan: ValidatedPlan) -> int:
    tracks = list(plan.resolved_tracks) if plan.resolved_tracks else resolve_timeline(plan.data)
    end = max((
        clip["timeline_start"] + clip["duration"]
        for track in tracks for clip in track["clips"] if clip.get("enabled", True)
    ), default=0)
    clips = {clip["id"]: clip for track in tracks for clip in track["clips"]}
    for operation in plan.data.get("operations", []):
        if operation.get("enabled", True) and operation.get("type") in {"caption", "overlay"}:
            clip = clips.get(operation.get("target"))
            if clip is not None:
                end = max(end, clip["timeline_start"] + operation.get("start", 0) + operation["duration"])
    return end


def _probe_render(path: Path, toolchain: Toolchain, supervisor: ProcessSupervisor) -> dict[str, Any]:
    result = supervisor.run([
        str(toolchain.ffprobe), "-v", "error", "-count_frames", "-show_format", "-show_streams", "-of", "json", str(path),
    ])
    if result.returncode:
        raise ExternalToolError(f"ffprobe rejected rendered output: {result.stderr or result.stdout}", code="render_probe_failed")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ExternalToolError("ffprobe returned invalid JSON for rendered output", code="render_probe_failed") from exc


def _reject_entirely_black_video(
    output: Path,
    *,
    duration_frames: int,
    frame_rate: Fraction,
    toolchain: Toolchain,
    supervisor: ProcessSupervisor,
) -> None:
    """Reject the blank-output failure mode without penalising short black edits."""
    result = supervisor.run([
        str(toolchain.ffmpeg), "-v", "info", "-i", str(output), "-map", "0:v:0", "-an",
        "-vf", "blackdetect=d=0.01:pix_th=0.10", "-f", "null", "-",
    ])
    if result.returncode:
        raise VideoEditingError("rendered output could not be checked for blank video", code="render_validation_failed")
    total_seconds = float(Fraction(duration_frames, 1) / frame_rate)
    tolerance = float(Fraction(2, 1) / frame_rate)
    for match in BLACK_SEGMENT_RE.finditer(f"{result.stdout}\n{result.stderr}"):
        if float(match.group("start")) <= tolerance and float(match.group("duration")) >= total_seconds - tolerance:
            raise VideoEditingError("rendered output is entirely black", code="render_validation_failed")


def validate_rendered_video(
    output: Path,
    plan: ValidatedPlan,
    toolchain: Toolchain,
    supervisor: ProcessSupervisor,
    workspace_root: Path,
) -> dict[str, Any]:
    if not output.is_file() or output.stat().st_size == 0:
        raise VideoEditingError("render output is missing or empty", code="render_missing_output")
    probe = _probe_render(output, toolchain, supervisor)
    video = next((stream for stream in probe.get("streams", []) if stream.get("codec_type") == "video"), None)
    if video is None:
        raise VideoEditingError("rendered output has no video stream", code="render_validation_failed")
    profile = plan.profile
    format_name = str(probe.get("format", {}).get("format_name", ""))
    if "mp4" not in format_name:
        raise VideoEditingError(f"rendered container is not MP4: {format_name or 'unknown'}", code="render_validation_failed")
    if video.get("codec_name") != "h264":
        raise VideoEditingError(f"rendered video codec is not H.264: {video.get('codec_name')}", code="render_validation_failed")
    if video.get("pix_fmt") != plan.data["export"].get("pixel_format", "yuv420p"):
        raise VideoEditingError("rendered pixel format does not match the edit plan", code="render_validation_failed")
    if video.get("width") != profile["width"] or video.get("height") != profile["height"]:
        raise VideoEditingError("rendered dimensions do not match the edit plan", code="render_validation_failed")
    expected_rate = Fraction(profile["frame_rate"]["numerator"], profile["frame_rate"]["denominator"])
    try:
        actual_rate = Fraction(video.get("avg_frame_rate") or video.get("r_frame_rate"))
    except (ValueError, ZeroDivisionError, TypeError) as exc:
        raise VideoEditingError("rendered output has no usable frame rate", code="render_validation_failed") from exc
    if actual_rate != expected_rate:
        raise VideoEditingError(f"rendered frame rate {actual_rate} does not match {expected_rate}", code="render_validation_failed")
    expected_frames = expected_output_frames(plan)
    actual_text = video.get("nb_read_frames") or video.get("nb_frames")
    if actual_text not in (None, "N/A"):
        actual_frames = int(actual_text)
    else:
        duration = probe.get("format", {}).get("duration") or video.get("duration")
        actual_frames = round(Fraction(str(duration)) * expected_rate)
    if abs(actual_frames - expected_frames) > 1:
        raise VideoEditingError(
            f"rendered frame count {actual_frames} differs from expected {expected_frames}",
            code="render_validation_failed",
        )
    _reject_entirely_black_video(
        output,
        duration_frames=actual_frames,
        frame_rate=actual_rate,
        toolchain=toolchain,
        supervisor=supervisor,
    )
    audio_stream = next((stream for stream in probe.get("streams", []) if stream.get("codec_type") == "audio"), None)
    resolved_tracks = list(plan.resolved_tracks) if plan.resolved_tracks else resolve_timeline(plan.data)
    expects_audio = any(
        not track.get("muted", False) and any(
            next((asset for asset in plan.data["assets"] if asset["id"] == clip["asset_id"]), {}).get("probe", {}).get("audio")
            for clip in track["clips"] if clip.get("enabled", True)
        )
        for track in resolved_tracks
    )
    if expects_audio and audio_stream is None:
        raise VideoEditingError("rendered output is missing expected audio", code="render_validation_failed")
    if audio_stream is not None and audio_stream.get("codec_name") != "aac":
        raise VideoEditingError(f"rendered audio codec is not AAC: {audio_stream.get('codec_name')}", code="render_validation_failed")
    if audio_stream is not None:
        if int(audio_stream.get("sample_rate", 0)) != profile["sample_rate"]:
            raise VideoEditingError("rendered audio sample rate does not match the edit plan", code="render_validation_failed")
        if audio_stream.get("channels") != profile["channels"]:
            raise VideoEditingError("rendered audio channel count does not match the edit plan", code="render_validation_failed")
    decoded = supervisor.run([
        str(toolchain.ffmpeg), "-v", "error", "-xerror", "-i", str(output), "-map", "0:v:0", "-map", "0:a?",
        "-progress", "pipe:1", "-nostats", "-f", "null", "-",
    ])
    if decoded.returncode:
        raise VideoEditingError(f"rendered output failed a complete decode: {decoded.stderr}", code="render_decode_failed")
    return {
        "status": "passed",
        "expected_frames": expected_frames,
        "actual_frames": actual_frames,
        "frame_rate": f"{actual_rate.numerator}/{actual_rate.denominator}",
        "video_codec": video.get("codec_name"),
        "audio_present": audio_stream is not None,
        "artifact": artifact_record(output, workspace_root),
    }
