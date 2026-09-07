"""Deterministic FFmpeg silence evidence and synchronized timeline cutting."""

from __future__ import annotations

import re
from copy import deepcopy
from fractions import Fraction
from pathlib import Path
from typing import Any

from .adaptive_silence import SilenceSettings, calibrate_noise, silence_policy


_EVENT = re.compile(r"silence_(start|end):\s*(-?\d+(?:\.\d+)?)")


def parse_silence_output(output: str, *, source: Path, frame_rate: Fraction,
                         threshold_db: float, minimum_seconds: float,
                         duration_seconds: str | Fraction | None = None) -> dict[str, Any]:
    """Convert FFmpeg diagnostics to rational-frame evidence."""
    starts: list[Fraction] = []
    intervals: list[dict[str, Any]] = []
    events = [(kind, Fraction(raw)) for kind, raw in _EVENT.findall(output)]
    if duration_seconds is not None:
        events.append(('end', Fraction(duration_seconds)))
    for kind, raw in events:
        seconds = Fraction(raw)
        if kind == "start": starts.append(seconds)
        elif starts:
            start = starts.pop(0)
            start_frame = max(0, round(start * frame_rate))
            end_frame = max(start_frame, round(seconds * frame_rate))
            if duration_seconds is not None:
                end_frame = min(end_frame, round(Fraction(duration_seconds) * frame_rate))
            if end_frame <= start_frame or seconds - start < Fraction(str(minimum_seconds)):
                continue
            intervals.append({"id": f"silence_{len(intervals):04d}", "start_frame": start_frame, "end_frame": end_frame,
                              "start_seconds": str(start), "end_seconds": str(seconds)})
    return {"kind": "ffmpeg_silencedetect", "source": str(source.resolve()),
            "settings": {"threshold_db": threshold_db, "minimum_seconds": minimum_seconds},
            "frame_rate": {"numerator": frame_rate.numerator, "denominator": frame_rate.denominator},
            "intervals": intervals}


def detect_silence(source: Path, *, frame_rate: Fraction, ffmpeg: str = "ffmpeg",
                   threshold_db: float | None = None, minimum_seconds: float = 0.5,
                   settings: SilenceSettings | None = None, duration_seconds=None,
                   supervisor=None) -> dict[str, Any]:
    from .adaptive_silence import analyze_audio
    return analyze_audio(source, frame_rate=frame_rate, ffmpeg=ffmpeg, threshold_db=threshold_db,
                         settings=settings or SilenceSettings(minimum_seconds=minimum_seconds),
                         duration_seconds=duration_seconds, supervisor=supervisor)


def apply_silence_removal(plan: dict[str, Any], *, asset_id: str, evidence: dict[str, Any],
                          padding_seconds: float = 0.12, crossfade_seconds: float = 0.04) -> dict[str, Any]:
    """Return a new plan whose linked A/V clips use identical frame-exact keep ranges.

    The source plan and media are untouched. Short fades are placed on both sides of
    each join; MLT mixes embedded audio with the same segment boundaries as video.
    """
    output = deepcopy(plan)
    rate_data = output["profile"]["frame_rate"]
    rate = Fraction(rate_data["numerator"], rate_data["denominator"])
    padding = max(0, round(Fraction(str(padding_seconds)) * rate))
    crossfade = max(0, round(Fraction(str(crossfade_seconds)) * rate))
    asset = next(item for item in output["assets"] if item["id"] == asset_id)
    duration = asset["duration_frames"]
    removed = []
    for item in evidence.get("intervals", []):
        begin = max(0, int(item["start_frame"]) + padding)
        end = min(duration, int(item["end_frame"]) - padding)
        if end > begin:
            removed.append((begin, end))
    removed.sort()
    keep: list[tuple[int, int]] = []
    cursor = 0
    for begin, end in removed:
        if begin > cursor: keep.append((cursor, begin))
        cursor = max(cursor, end)
    if cursor < duration: keep.append((cursor, duration))
    new_operations = list(output.get("operations", []))
    for track in output["tracks"]:
        replaced = []
        for clip in track["clips"]:
            if clip["asset_id"] != asset_id:
                replaced.append(clip); continue
            clip_begin, clip_end = clip["source_in"], clip["source_in"] + clip["duration"]
            timeline = clip["timeline_start"]
            part = 0
            for begin, end in keep:
                begin, end = max(begin, clip_begin), min(end, clip_end)
                if end <= begin: continue
                derived = deepcopy(clip)
                derived.update(id=f"{clip['id']}__speech_{part:03d}", timeline_start=timeline,
                               source_in=begin, duration=end - begin)
                if part and crossfade and replaced:
                    previous = replaced[-1]
                    fade = min(crossfade, previous["duration"])
                    new_operations.append({"id": f"silence_join_out_{previous['id']}", "type": "fade_audio",
                                           "target": previous["id"], "start": previous["duration"] - fade,
                                           "duration": fade, "direction": "out", "from": 1.0, "to": 0.0})
                replaced.append(derived)
                if part and crossfade:
                    fade = min(crossfade, derived["duration"])
                    new_operations.append({"id": f"silence_join_in_{derived['id']}", "type": "fade_audio",
                                           "target": derived["id"], "start": 0, "duration": fade,
                                           "direction": "in", "from": 0.0, "to": 1.0})
                timeline += end - begin
                part += 1
        track["clips"] = replaced
    output["operations"] = new_operations
    output["analysis"] = {**output.get("analysis", {}), "silence": evidence,
                          "silence_removal": {"asset_id": asset_id, "padding_frames": padding,
                                              "crossfade_frames": crossfade, "removed_intervals": removed}}
    return output
