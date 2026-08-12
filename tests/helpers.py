from __future__ import annotations

from pathlib import Path
from typing import Any


def valid_plan(asset: Path) -> dict[str, Any]:
    return {
        "version": "1.0",
        "profile": {
            "width": 1920, "height": 1080,
            "frame_rate": {"numerator": 30000, "denominator": 1001},
            "sample_rate": 48000, "channels": 2,
        },
        "assets": [{
            "id": "a", "path": asset.name, "kind": "video", "duration_frames": 300,
            "fingerprint": "sha256:abc", "probe": {},
        }],
        "tracks": [{
            "id": "v1", "kind": "video", "name": "Vidéos",
            "clips": [{"id": "c1", "asset_id": "a", "timeline_start": 0, "source_in": 0, "duration": 100}],
        }],
        "operations": [],
        "export": {"format": "mp4", "video_codec": "libx264", "audio_codec": "aac"},
    }

