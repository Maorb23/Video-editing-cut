"""Immutable, evidence-oriented video and audio inspection passes."""

from __future__ import annotations

import json
import re
import shutil
from fractions import Fraction
from pathlib import Path
from typing import Any

from .errors import VideoEditingError
from .plan import read_json
from .process import run_checked


IDENTITY_GEOMETRY = "0%/0%:100%x100%"
IDENTICAL_SSIM_THRESHOLD = 0.995
SSIM_RE = re.compile(r"All:([0-9.]+)")


def _next_pass(output_dir: Path) -> tuple[int, Path]:
    passes = output_dir / "passes"
    passes.mkdir(parents=True, exist_ok=True)
    numbers = []
    for child in passes.iterdir():
        if child.is_dir() and child.name.startswith("pass-"):
            try:
                numbers.append(int(child.name.removeprefix("pass-")))
            except ValueError:
                pass
    number = max(numbers, default=0) + 1
    destination = passes / f"pass-{number:03d}"
    destination.mkdir()
    return number, destination


def _probe(video: Path, ffprobe: str) -> dict[str, Any]:
    result = run_checked([ffprobe, "-v", "error", "-show_format", "-show_streams", "-of", "json", str(video)])
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise VideoEditingError("ffprobe returned invalid JSON", code="invalid_probe") from exc


def _rate_and_duration(probe: dict[str, Any]) -> tuple[Fraction, int, dict[str, Any] | None]:
    video_stream = next((stream for stream in probe.get("streams", []) if stream.get("codec_type") == "video"), None)
    if video_stream is None:
        raise VideoEditingError("inspection requires a video stream", code="missing_video_stream")
    rate_text = video_stream.get("avg_frame_rate") or video_stream.get("r_frame_rate")
    if not rate_text or rate_text == "0/0":
        raise VideoEditingError("video has no usable frame rate", code="invalid_probe")
    rate = Fraction(rate_text)
    frame_count = video_stream.get("nb_frames")
    if isinstance(frame_count, str) and frame_count.isdigit() and int(frame_count) > 0:
        duration_frames = int(frame_count)
    else:
        duration_text = probe.get("format", {}).get("duration") or video_stream.get("duration")
        if not duration_text:
            raise VideoEditingError("video has no usable duration", code="invalid_probe")
        duration_frames = max(1, int(Fraction(duration_text) * rate))
    audio = next((stream for stream in probe.get("streams", []) if stream.get("codec_type") == "audio"), None)
    return rate, duration_frames, audio


def _sample_frame_reasons(
    duration_frames: int,
    boundaries: set[int],
    keyframes: set[int],
    rate: Fraction,
) -> dict[int, list[str]]:
    reasons: dict[int, set[str]] = {}

    def add(frame: int, reason: str) -> None:
        if 0 <= frame < duration_frames:
            reasons.setdefault(frame, set()).add(reason)

    regular_step = max(1, round(rate))
    add(0, "regular")
    add(duration_frames - 1, "regular")
    for frame in range(0, duration_frames, regular_step):
        add(frame, "regular")
    for boundary in boundaries:
        for frame in (boundary - 1, boundary, boundary + 1):
            add(frame, "boundary")
    for keyframe in keyframes:
        for frame in (keyframe - 1, keyframe, keyframe + 1):
            add(frame, "keyframe")
    return {frame: sorted(values) for frame, values in sorted(reasons.items())}


def _sample_frames(
    duration_frames: int,
    boundaries: set[int],
    rate: Fraction,
    keyframes: set[int] | None = None,
) -> list[tuple[int, str]]:
    """Return de-duplicated samples while retaining the historical tuple API."""
    priorities = ("keyframe", "boundary", "regular")
    reasons = _sample_frame_reasons(duration_frames, boundaries, keyframes or set(), rate)
    return [(frame, next(reason for reason in priorities if reason in values)) for frame, values in reasons.items()]


def _extract_frame(video: Path, frame: int, destination: Path, ffmpeg: str) -> None:
    # select evaluates decoded frame numbers, avoiding timestamp rounding and seek placement.
    run_checked([
        ffmpeg, "-v", "error", "-i", str(video), "-vf", f"select=eq(n\\,{frame})",
        "-frames:v", "1", "-fps_mode", "vfr", str(destination),
    ])
    if not destination.is_file():
        raise VideoEditingError(f"ffmpeg did not extract frame {frame} from {video}", code="frame_extraction_failed")


def _clip_lookup(plan: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        clip["id"]: clip
        for track in plan.get("tracks", [])
        for clip in track.get("clips", [])
        if isinstance(clip, dict) and isinstance(clip.get("id"), str)
    }


def _transform_keyframes(plan: dict[str, Any], duration_frames: int) -> list[dict[str, Any]]:
    clips = _clip_lookup(plan)
    evidence: list[dict[str, Any]] = []
    for operation in plan.get("operations", []):
        if operation.get("type") != "transform" or not operation.get("enabled", True):
            continue
        clip = clips.get(operation.get("target"))
        if clip is None:
            continue
        state: dict[str, Any] = {
            "geometry": operation.get("geometry", IDENTITY_GEOMETRY),
            "opacity": operation.get("opacity", 1),
            "rotation": operation.get("rotation", 0),
        }
        operation_start = operation.get("start", 0)
        for index, keyframe in enumerate(operation.get("keyframes", [])):
            if not isinstance(keyframe, dict) or not isinstance(keyframe.get("frame"), int):
                continue
            state.update({name: keyframe[name] for name in ("geometry", "opacity") if name in keyframe})
            local_frame = operation_start + keyframe["frame"]
            timeline_frame = clip.get("timeline_start", 0) + local_frame
            required = [
                frame for frame in (timeline_frame - 1, timeline_frame, timeline_frame + 1)
                if 0 <= frame < duration_frames
            ]
            expected_non_identity = (
                state["geometry"] != IDENTITY_GEOMETRY
                or state["opacity"] != 1
                or state["rotation"] != 0
            )
            evidence.append({
                "operation_id": operation.get("id"),
                "keyframe_index": index,
                "keyframe_frame": keyframe["frame"],
                "operation_start": operation_start,
                "timeline_frame": timeline_frame,
                "source_frame": clip.get("source_in", 0) + local_frame,
                "asset_id": clip.get("asset_id"),
                "required_frames": required,
                "expected": dict(state),
                "expected_non_identity": expected_non_identity,
            })
    return evidence


def _compare_frames(rendered: Path, source: Path, ffmpeg: str) -> float:
    result = run_checked([
        ffmpeg, "-v", "info", "-i", str(rendered), "-i", str(source),
        "-lavfi", "[1:v][0:v]scale2ref[reference][rendered];[rendered][reference]ssim", "-f", "null", "-",
    ])
    matches = SSIM_RE.findall(result.stderr)
    if not matches:
        raise VideoEditingError("ffmpeg did not report an SSIM value", code="comparison_failed")
    return float(matches[-1])


def evaluate_inspection(record: dict[str, Any]) -> dict[str, Any]:
    """Derive pass status; a stored status value alone can never override blockers."""
    blockers: list[dict[str, Any]] = []
    requirements = record.get("evidence_requirements", {})
    expected_keyframes = requirements.get("transform_keyframes")
    coverage_records = record.get("keyframe_coverage", [])
    findings = record.get("automated_findings", [])
    if isinstance(expected_keyframes, int) and len(coverage_records) != expected_keyframes:
        blockers.append({"code": "missing_keyframe_evidence", "expected": expected_keyframes, "actual": len(coverage_records)})
    if isinstance(expected_keyframes, int) and len(findings) != expected_keyframes:
        blockers.append({"code": "missing_automated_finding", "expected": expected_keyframes, "actual": len(findings)})
    for coverage in coverage_records:
        missing = sorted(set(coverage.get("required_frames", [])) - set(coverage.get("sampled_frames", [])))
        if missing or coverage.get("status") != "complete":
            blockers.append({"code": "missing_keyframe_evidence", "operation_id": coverage.get("operation_id"), "frames": missing})
    for frame in record.get("frames", []):
        review_status = frame.get("review", {}).get("status")
        if review_status != "pass":
            blockers.append({"code": "frame_review_not_passed", "frame": frame.get("frame"), "status": review_status})
    audio_review = record.get("audio", {}).get("review")
    if isinstance(audio_review, dict) and audio_review.get("status") != "pass":
        blockers.append({"code": "audio_review_not_passed", "status": audio_review.get("status")})
    for finding in findings:
        if finding.get("status") not in {"pass", "not_applicable"}:
            blockers.append({"code": finding.get("code", "automated_check_not_passed"), "status": finding.get("status")})

    failed_codes = {"effect_not_applied", "comparison_failed", "source_evidence_missing"}
    failed = any(item.get("code") in failed_codes or item.get("status") == "fail" for item in blockers)
    return {"status": "failed" if failed else ("passed" if not blockers else "pending"), "eligible": not blockers, "blockers": blockers}


def check_inspection(record_path: Path) -> dict[str, Any]:
    record = read_json(record_path)
    evaluation = evaluate_inspection(record)
    if not evaluation["eligible"]:
        raise VideoEditingError(
            f"inspection pass is {evaluation['status']} with {len(evaluation['blockers'])} blocker(s)",
            code="inspection_not_passed",
        )
    return evaluation


def inspect(
    video: Path,
    edit_plan: Path,
    output_dir: Path,
    *,
    ffmpeg: str | None = None,
    ffprobe: str | None = None,
    comparison_sources: dict[str, Path] | None = None,
) -> Path:
    if not video.is_file():
        raise VideoEditingError(f"video not found: {video}", code="missing_file")
    ffmpeg_bin = ffmpeg or shutil.which("ffmpeg") or shutil.which("ffmpeg.exe")
    ffprobe_bin = ffprobe or shutil.which("ffprobe") or shutil.which("ffprobe.exe")
    if not ffmpeg_bin or not ffprobe_bin:
        raise VideoEditingError("ffmpeg and ffprobe are required for inspection", code="tool_unavailable")
    plan = read_json(edit_plan)
    probe = _probe(video, ffprobe_bin)
    rate, duration_frames, audio_stream = _rate_and_duration(probe)
    boundaries: set[int] = set()
    clips = _clip_lookup(plan)
    for clip in clips.values():
        start = clip.get("timeline_start")
        length = clip.get("duration")
        if isinstance(start, int) and isinstance(length, int):
            boundaries.update({start, start + length})
    for operation in plan.get("operations", []):
        if isinstance(operation.get("start"), int):
            clip = clips.get(operation.get("target"))
            timeline_start = operation["start"] + (clip.get("timeline_start", 0) if clip else 0)
            boundaries.add(timeline_start)
            if isinstance(operation.get("duration"), int):
                boundaries.add(timeline_start + operation["duration"])

    keyframe_coverage = _transform_keyframes(plan, duration_frames)
    keyframe_frames = {item["timeline_frame"] for item in keyframe_coverage}
    sample_reasons = _sample_frame_reasons(duration_frames, boundaries, keyframe_frames, rate)
    pass_number, pass_dir = _next_pass(output_dir)
    frames_dir = pass_dir / "frames"
    frames_dir.mkdir()
    records: list[dict[str, Any]] = []
    for frame, reasons in sample_reasons.items():
        timestamp = Fraction(frame, 1) / rate
        destination = frames_dir / f"frame-{frame:08d}.png"
        _extract_frame(video, frame, destination, ffmpeg_bin)
        records.append({
            "frame": frame,
            "timestamp_seconds": f"{float(timestamp):.9f}",
            "reason": "keyframe" if "keyframe" in reasons else ("boundary" if "boundary" in reasons else "regular"),
            "reasons": reasons,
            "path": str(destination.resolve()),
            "review": {"status": "pending", "observation": "", "severity": None, "source_action": None, "planned_change": None},
        })

    sampled = set(sample_reasons)
    for coverage in keyframe_coverage:
        coverage["sampled_frames"] = [frame for frame in coverage["required_frames"] if frame in sampled]
        coverage["status"] = "complete" if coverage["sampled_frames"] == coverage["required_frames"] else "missing"

    assets = {item.get("id"): item for item in plan.get("assets", []) if isinstance(item, dict)}
    source_frames_dir = pass_dir / "source-frames"
    automated_findings: list[dict[str, Any]] = []
    for finding_index, coverage in enumerate(keyframe_coverage):
        finding = {
            "operation_id": coverage["operation_id"],
            "keyframe_index": coverage["keyframe_index"],
            "timeline_frame": coverage["timeline_frame"],
            "code": "transform_conformance",
        }
        if not coverage["expected_non_identity"]:
            finding.update({"status": "not_applicable", "message": "identity transform expected at this keyframe"})
            automated_findings.append(finding)
            continue
        asset = assets.get(coverage["asset_id"])
        source = (comparison_sources or {}).get(str(coverage["asset_id"]))
        if source is None and asset is not None:
            source = (edit_plan.resolve().parent / str(asset.get("path"))).resolve()
        if source is None or not source.is_file():
            finding.update({"status": "fail", "code": "source_evidence_missing", "message": "source media is unavailable for conformance comparison"})
            automated_findings.append(finding)
            continue
        source_frames_dir.mkdir(exist_ok=True)
        source_destination = source_frames_dir / f"keyframe-{finding_index:08d}-source-{coverage['source_frame']:08d}.png"
        try:
            _extract_frame(source, coverage["source_frame"], source_destination, ffmpeg_bin)
            rendered_destination = frames_dir / f"frame-{coverage['timeline_frame']:08d}.png"
            score = _compare_frames(rendered_destination, source_destination, ffmpeg_bin)
        except VideoEditingError as exc:
            finding.update({"status": "fail", "code": "comparison_failed", "message": str(exc)})
        else:
            finding.update({
                "status": "fail" if score >= IDENTICAL_SSIM_THRESHOLD else "pass",
                "code": "effect_not_applied" if score >= IDENTICAL_SSIM_THRESHOLD else "transform_applied",
                "message": "rendered and source frames are effectively identical" if score >= IDENTICAL_SSIM_THRESHOLD else "rendered frame differs from source as expected",
                "metric": {"name": "ssim", "value": score, "identical_threshold": IDENTICAL_SSIM_THRESHOLD},
                "rendered_frame_path": str(rendered_destination.resolve()),
                "source_frame_path": str(source_destination.resolve()),
            })
        automated_findings.append(finding)

    from .grading import inspect_hue_grades
    automated_findings.extend(inspect_hue_grades(plan, video, edit_plan, pass_dir, ffmpeg_bin))
    audio_evidence: dict[str, Any] = {
        "present": audio_stream is not None,
        "review": {"status": "pending", "observation": "", "severity": None, "source_action": None, "planned_change": None},
    }
    if audio_stream is not None:
        analyses = {
            "volume": ["-af", "volumedetect"],
            "silence": ["-af", "silencedetect=noise=-50dB:d=0.25"],
            "loudness": ["-filter_complex", "ebur128=peak=true"],
        }
        for name, filter_args in analyses.items():
            result = run_checked([ffmpeg_bin, "-hide_banner", "-i", str(video), *filter_args, "-f", "null", "-"])
            audio_evidence[name] = result.stderr[-12000:]
        mean_match = re.findall(r"mean_volume:\s*(-?[\d.]+) dB", audio_evidence["volume"])
        peak_match = re.findall(r"max_volume:\s*(-?[\d.]+) dB", audio_evidence["volume"])
        silence_ends = re.findall(r"silence_end:\s*([\d.]+)", audio_evidence["silence"])
        audio_evidence["measurements"] = {
            "dereverb_provenance": plan.get("analysis", {}).get("dereverb"),
            "mean_db": float(mean_match[-1]) if mean_match else None,
            "peak_db": float(peak_match[-1]) if peak_match else None,
            "last_silence_end_seconds": float(silence_ends[-1]) if silence_ends else None,
            "effects": [
                {"operation_id": op.get("id"), "type": op.get("type"),
                 "decision": "derived immutable audio" if op.get("type") == "dereverb" else "curated MLT avfilter"}
                for op in plan.get("operations", []) if op.get("type") in {"parametric_eq", "reverb", "dereverb"}
            ],
        }
        waveform = pass_dir / "waveform.png"
        run_checked([ffmpeg_bin, "-v", "error", "-i", str(video), "-filter_complex", "showwavespic=s=1600x320:colors=0x33aaff", "-frames:v", "1", str(waveform)])
        audio_evidence["waveform"] = str(waveform.resolve())

    record = {
        "version": "1.1", "pass": pass_number,
        "video": str(video.resolve()), "edit_plan": str(edit_plan.resolve()),
        "profile": {"frame_rate": {"numerator": rate.numerator, "denominator": rate.denominator}, "duration_frames": duration_frames},
        "frames": records,
        "evidence_requirements": {"transform_keyframes": len(keyframe_coverage)},
        "keyframe_coverage": keyframe_coverage,
        "automated_findings": automated_findings,
        "audio": audio_evidence,
        "instructions": "Inspect every persisted sample. A pass is eligible only when every review passes, keyframe coverage is complete, and automated findings pass.",
    }
    evaluation = evaluate_inspection(record)
    record["status"] = evaluation["status"]
    record["eligibility"] = evaluation
    record_path = pass_dir / "inspection.json"
    record_path.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    metadata = {
        "version": "1.1", "latest_pass": pass_number, "latest_inspection": str(record_path.resolve()),
        "video": str(video.resolve()), "probe": probe,
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "metadata.json").write_text(json.dumps(metadata, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return record_path
