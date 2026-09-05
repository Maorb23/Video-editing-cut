"""Media probing and stable content manifests."""

from __future__ import annotations

import hashlib
import json
import shutil
from fractions import Fraction
from pathlib import Path
from typing import Any

from .errors import VideoEditingError
from .process import run_checked
from .supervisor import ProcessSupervisor


def fingerprint(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(chunk_size):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def probe_one(path: Path, *, ffprobe: str | None = None, supervisor: ProcessSupervisor | None = None) -> dict[str, Any]:
    if not path.is_file():
        raise VideoEditingError(f"media file not found: {path}", code="missing_asset")
    binary = ffprobe or shutil.which("ffprobe") or shutil.which("ffprobe.exe")
    if not binary:
        raise VideoEditingError("ffprobe is required; run check_environment.py for guidance", code="tool_unavailable")
    arguments = [binary, "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)]
    if supervisor is None:
        result = run_checked(arguments)
    else:
        result = supervisor.run(arguments)
        if result.returncode:
            raise VideoEditingError(f"ffprobe failed for {path}: {result.stderr or result.stdout}", code="invalid_probe")
    try:
        raw = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise VideoEditingError(f"ffprobe returned invalid JSON for {path}", code="invalid_probe") from exc
    streams = raw.get("streams", [])
    video = next((item for item in streams if item.get("codec_type") == "video"), None)
    audio = next((item for item in streams if item.get("codec_type") == "audio"), None)
    kind = "video" if video else "audio" if audio else "image" if streams else "unknown"
    duration_text = raw.get("format", {}).get("duration") or (video or audio or {}).get("duration")
    duration = str(Fraction(duration_text)) if duration_text not in (None, "N/A") else None
    selected_rate = (video or {}).get("avg_frame_rate")
    return {
        "id": path.stem,
        "path": str(path),
        "kind": kind,
        "size_bytes": path.stat().st_size,
        "fingerprint": fingerprint(path),
        "duration_seconds": duration,
        "video": None if video is None else {
            "codec": video.get("codec_name"), "width": video.get("width"), "height": video.get("height"),
            "avg_frame_rate": selected_rate, "r_frame_rate": video.get("r_frame_rate"),
            "pixel_format": video.get("pix_fmt"), "time_base": video.get("time_base"),
        },
        "audio": None if audio is None else {
            "codec": audio.get("codec_name"), "sample_rate": audio.get("sample_rate"),
            "channels": audio.get("channels"), "channel_layout": audio.get("channel_layout"),
        },
        "probe": raw,
    }


def create_manifest(paths: list[Path], *, ffprobe: str | None = None) -> dict[str, Any]:
    assets = [probe_one(path, ffprobe=ffprobe) for path in paths]
    used: dict[str, int] = {}
    for asset in assets:
        base = asset["id"] or "asset"
        used[base] = used.get(base, 0) + 1
        if used[base] > 1:
            asset["id"] = f"{base}-{used[base]}"
    return {"version": "1.0", "assets": assets}
