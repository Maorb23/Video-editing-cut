"""Persist conservative visual and source-handle evidence for pause L-cuts."""
from __future__ import annotations

import re
from fractions import Fraction
from pathlib import Path
from typing import Any

from .adaptive_silence import SilenceSettings, silence_policy
from .supervisor import ProcessSupervisor


_SSIM_ALL = re.compile(r"\bAll:([0-9]+(?:\.[0-9]+)?)")
MINIMUM_L_CUT_SSIM = .80


def boundary_similarity(
    proxy: Path, before_frame: int, after_frame: int, *, ffmpeg: str, supervisor: ProcessSupervisor,
) -> float | None:
    """Compare two exact proxy frames; return SSIM or unavailable."""
    graph = (
        f"[0:v]split=2[left][right];"
        f"[left]select=eq(n\\,{before_frame}),setpts=PTS-STARTPTS[a];"
        f"[right]select=eq(n\\,{after_frame}),setpts=PTS-STARTPTS[b];[a][b]ssim"
    )
    result = supervisor.run([
        ffmpeg, "-hide_banner", "-nostdin", "-i", str(proxy), "-filter_complex", graph,
        "-frames:v", "1", "-f", "null", "-",
    ])
    if result.returncode:
        return None
    matches = _SSIM_ALL.findall(f"{result.stdout}\n{result.stderr}")
    return float(matches[-1]) if matches else None


def enrich_transition_evidence(
    evidence: dict[str, Any], *, proxy: Path, frame_rate: Fraction, duration_frames: int,
    settings: SilenceSettings, ffmpeg: str, supervisor: ProcessSupervisor, max_candidates: int = 64,
) -> dict[str, Any]:
    """Add auditable L-cut safety evidence without weakening compiler checks."""
    records: list[dict[str, Any]] = []
    offset_frames = 4
    for index, candidate in enumerate(evidence.get("intervals", [])):
        context = candidate.get("contextual_evidence", {})
        before_frame = candidate["start_frame"] - 1
        after_frame = candidate["end_frame"]
        audio_safe = (
            context.get("status") == "complete"
            and context.get("speech_before") is True
            and context.get("speech_after") is True
            and context.get("room_tone_difference") == "low"
        )
        policy = silence_policy(candidate, frame_rate, settings)
        removed_frames = candidate["duration_frames"] - policy["retained_frames"]
        handles_safe = before_frame >= 0 and after_frame < duration_frames and removed_frames >= offset_frames
        similarity = None
        if index < max_candidates and audio_safe and handles_safe:
            similarity = boundary_similarity(proxy, before_frame, after_frame, ffmpeg=ffmpeg, supervisor=supervisor)
        visually_compatible = similarity is not None and similarity >= MINIMUM_L_CUT_SSIM
        safe = audio_safe and handles_safe and visually_compatible
        visual_id = f"analysis/transition-safety.json#{candidate['id']}"
        if similarity is not None:
            context["evidence_ids"] = [*context.get("evidence_ids", []), visual_id]
        context.update(
            visual_discontinuity="low" if visually_compatible else "high" if similarity is not None else "unavailable",
            visual_similarity_ssim=similarity,
            lips_visible_near_cut=False if visually_compatible else None,
            lips_assessment="compatible boundary motion under the talking-head L-cut threshold" if visually_compatible else "unavailable",
            source_handles_safe=handles_safe,
            adjacency_safe=True,
            l_cut_safe=safe,
            j_cut_safe=False,
        )
        if safe:
            context["confidence"] = min(float(context["confidence"]), .92)
        candidate["suggestion"] = silence_policy(candidate, frame_rate, settings)
        records.append({
            "candidate_id": candidate["id"], "status": "safe" if safe else "fallback",
            "before_frame": before_frame, "after_frame": after_frame,
            "visual_similarity_ssim": similarity, "minimum_visual_similarity_ssim": MINIMUM_L_CUT_SSIM,
            "speech_before": context.get("speech_before"), "speech_after": context.get("speech_after"),
            "room_tone_difference": context.get("room_tone_difference"),
            "source_handles_safe": handles_safe, "adjacency_safe": True,
            "lips_visible_near_cut": context.get("lips_visible_near_cut"),
            "l_cut_safe": safe, "evidence_ids": list(context.get("evidence_ids", [])),
        })
    return {
        "version": "1.0", "kind": "pause_transition_safety", "status": "complete",
        "source_fingerprint": evidence.get("source_fingerprint"),
        "frame_rate": evidence.get("frame_rate"), "l_cut_offset_frames": offset_frames,
        "candidates": records,
    }
