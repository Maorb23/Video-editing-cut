"""Deterministic CFR proxy and bounded visual-evidence extraction."""

from __future__ import annotations

import json
from dataclasses import asdict
from fractions import Fraction
from pathlib import Path
from typing import Any

from ..errors import ExternalToolError, VideoEditingError
from ..probe import probe_one
from ..supervisor import ProcessSupervisor, Toolchain
from ..timebase import seconds_to_frames
from ..workspace import JobWorkspace
from ..silence import detect_silence, SilenceSettings
from .base import AnalysisArtifact


def project_duration_frames(duration_seconds: str | Fraction, frame_rate: Fraction) -> int:
    """Map timestamps to the CFR project without using decoded VFR frame indices."""
    return max(1, seconds_to_frames(duration_seconds, frame_rate.numerator, frame_rate.denominator))


def source_frame_rate(video: dict[str, Any]) -> Fraction:
    for candidate in (video.get("avg_frame_rate"), video.get("r_frame_rate")):
        try:
            rate = Fraction(candidate)
            if rate > 0:
                return rate
        except (ValueError, TypeError, ZeroDivisionError):
            continue
    raise VideoEditingError("source has no usable rational frame rate", code="unsupported_media")


def sample_project_frames(duration_frames: int, max_samples: int) -> list[int]:
    count = min(max_samples, duration_frames)
    if count == 1:
        return [0]
    return sorted({round(Fraction(index * (duration_frames - 1), count - 1)) for index in range(count)})


def _decoded_video_frame_count(
    path: Path,
    *,
    toolchain: Toolchain,
    supervisor: ProcessSupervisor,
) -> int:
    """Return the number of frames ffprobe can decode from the first video stream."""
    result = supervisor.run([
        str(toolchain.ffprobe), "-v", "error", "-select_streams", "v:0", "-count_frames",
        "-show_entries", "stream=nb_read_frames", "-of", "json", str(path),
    ])
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip() or "no diagnostic output"
        raise ExternalToolError(f"analysis proxy frame count failed: {detail}", code="analysis_failed")
    try:
        streams = json.loads(result.stdout).get("streams", [])
        frame_count = int(streams[0]["nb_read_frames"])
    except (json.JSONDecodeError, IndexError, KeyError, TypeError, ValueError) as exc:
        raise VideoEditingError(
            "analysis proxy has no usable decoded video frame count",
            code="analysis_failed",
        ) from exc
    if frame_count < 1:
        raise VideoEditingError("analysis proxy contains no decodable video frames", code="analysis_failed")
    return frame_count


class FrameAnalysisProvider:
    MAX_SAMPLES = 24
    MAX_PROXY_WIDTH = 1280

    def __init__(self, *, frame_rate: Fraction | None = None, max_samples: int = 12, proxy_width: int = 640,
                 silence_settings: SilenceSettings) -> None:
        if (frame_rate is not None and frame_rate <= 0) or not 1 <= max_samples <= self.MAX_SAMPLES or not 1 <= proxy_width <= self.MAX_PROXY_WIDTH:
            raise ValueError(
                f"analysis settings require a positive frame rate, 1-{self.MAX_SAMPLES} samples, "
                f"and a 1-{self.MAX_PROXY_WIDTH}px proxy width"
            )
        self.frame_rate = frame_rate
        self.max_samples = max_samples
        self.proxy_width = proxy_width
        self.silence_settings = silence_settings

    @property
    def version(self) -> str:
        return "cfr-contact-sheet/1.0"

    def _run(self, supervisor: ProcessSupervisor, arguments: list[str], stage: str) -> None:
        result = supervisor.run(arguments, progress_predicate=lambda line: "progress=" in line)
        if result.returncode:
            detail = result.stderr.strip() or result.stdout.strip() or "no diagnostic output"
            raise ExternalToolError(f"{stage} failed: {detail}", code="analysis_failed")

    def analyze(
        self,
        source: Path,
        workspace: JobWorkspace,
        toolchain: Toolchain,
        supervisor: ProcessSupervisor,
    ) -> AnalysisArtifact:
        source = workspace.assert_confined(source)
        media = probe_one(source, ffprobe=str(toolchain.ffprobe), supervisor=supervisor)
        if media.get("video") is None or media.get("duration_seconds") is None:
            raise VideoEditingError("standalone Phase 0 requires a video stream with known duration", code="unsupported_media")
        duration_seconds = Fraction(media["duration_seconds"])
        frame_rate = self.frame_rate
        if frame_rate is None:
            frame_rate = source_frame_rate(media["video"])
        proxy = workspace.path("analysis/proxy.mp4")
        rate_text = f"{frame_rate.numerator}/{frame_rate.denominator}"
        scale = f"scale='min({self.proxy_width},iw)':-2"
        self._run(supervisor, [
            str(toolchain.ffmpeg), "-v", "error", "-i", str(source), "-map", "0:v:0",
            "-vf", f"fps={rate_text},{scale}", "-an", "-c:v", "libx264", "-preset", "veryfast",
            "-crf", "30", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
            "-progress", "pipe:1", "-nostats", str(proxy),
        ], "CFR proxy generation")
        if not proxy.is_file() or proxy.stat().st_size == 0:
            raise VideoEditingError("analysis proxy was not produced", code="analysis_failed")

        # Container duration can include longer non-video streams. Count the
        # completed video-only proxy by decoding it, so every requested sample
        # is guaranteed to be within the proxy's actual frame domain.
        duration_frames = _decoded_video_frame_count(proxy, toolchain=toolchain, supervisor=supervisor)
        frames = sample_project_frames(duration_frames, self.max_samples)
        frames_dir = workspace.path("analysis/frames")
        frames_dir.mkdir()
        expression = "+".join(f"eq(n\\,{frame})" for frame in frames)
        pattern = frames_dir / "sample-%03d.jpg"
        self._run(supervisor, [
            str(toolchain.ffmpeg), "-v", "error", "-i", str(proxy), "-vf", f"select={expression}",
            "-fps_mode", "vfr", "-q:v", "3", "-progress", "pipe:1", "-nostats", str(pattern),
        ], "analysis frame extraction")
        extracted = tuple(sorted(frames_dir.glob("sample-*.jpg")))
        if len(extracted) != len(frames):
            raise VideoEditingError(
                f"expected {len(frames)} analysis frames but extracted {len(extracted)}",
                code="analysis_failed",
            )

        avg_rate = media["video"].get("avg_frame_rate")
        nominal_rate = media["video"].get("r_frame_rate")
        observations: list[dict[str, Any]] = []
        for frame, path in zip(frames, extracted):
            observations.append({
                "project_frame": frame,
                "timestamp_seconds": f"{float(Fraction(frame, 1) / frame_rate):.9f}",
                "path": path.relative_to(workspace.root).as_posix(),
            })
        data = {
            "version": "1.0",
            "provider": self.version,
            "analysis_configuration": {"silence": asdict(self.silence_settings)},
            "timeline_policy": {
                "name": "cfr_analysis_proxy",
                "frame_rate": {"numerator": frame_rate.numerator, "denominator": frame_rate.denominator},
                "duration_frames": duration_frames,
                "source_avg_frame_rate": avg_rate,
                "source_nominal_frame_rate": nominal_rate,
                "source_is_vfr": bool(avg_rate and nominal_rate and avg_rate != nominal_rate),
            },
            "source": {
                "id": "source",
                "kind": "video",
                "duration_seconds": media["duration_seconds"],
                "duration_frames": duration_frames,
                "fingerprint": media["fingerprint"],
                "video": media["video"],
                "audio": media["audio"],
            },
            "proxy": proxy.relative_to(workspace.root).as_posix(),
            "observations": observations,
        }
        if media.get("audio") is not None:
            silence = detect_silence(source, frame_rate=frame_rate, ffmpeg=str(toolchain.ffmpeg),
                                     settings=self.silence_settings, duration_seconds=duration_seconds,
                                     supervisor=supervisor, source_fingerprint=media['fingerprint'])
            silence.update(asset_id='source')
            silence_path = workspace.write_json("analysis/silence.json", silence)
            from ..silence_edits import silence_review_markdown
            review_path = workspace.path('analysis/detected-silences.md')
            with review_path.open('x', encoding='utf-8') as stream:
                stream.write(silence_review_markdown(silence, frame_rate))
            data["audio_evidence"] = {"silence": silence,
                                      "evidence_id": silence_path.relative_to(workspace.root).as_posix()}
            data.setdefault("preflight", {})["silence"] = {
                "status": "complete",
                "evidence_id": silence_path.relative_to(workspace.root).as_posix(),
                "minimum_silence_seconds": self.silence_settings.minimum_silence_seconds,
                "analyzed_duration_seconds": silence["analyzed_duration_seconds"],
            }
        path = workspace.write_json("analysis/analysis.json", data)
        return AnalysisArtifact(data, path, extracted)
