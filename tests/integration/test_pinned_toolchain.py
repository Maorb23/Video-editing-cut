from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Any

from video_editing.inspect import inspect
from video_editing.pipeline import run_pipeline
from video_editing.planning import ModelResponse
from video_editing.process import run_checked


class ZoomModel:
    def generate(self, **kwargs: Any) -> ModelResponse:
        request = json.loads(kwargs["input_text"])
        duration = request["analysis"]["source"]["duration_frames"]
        return ModelResponse({
            "summary": "Keep the complete source and apply a visible zoom.", "unsupported": [],
            "tracks": [{
                "id": "v1", "kind": "video", "name": "Video", "muted": False, "hidden": False,
                "clips": [{"id": "c1", "asset_id": "source", "timeline_start": 0, "source_in": 0, "duration": duration, "enabled": True}],
            }],
            "operations": [{
                "id": "zoom", "type": "transform", "target": "c1", "start": 0, "duration": duration,
                "keyframes": [
                    {"frame": 0, "geometry": "0%/0%:100%x100%"},
                    {"frame": duration // 2, "geometry": "-25%/-25%:150%x150%"},
                ],
                "enabled": True,
            }],
            "export": {
                "format": "mp4", "video_codec": "libx264", "audio_codec": "aac", "video_bitrate": None,
                "audio_bitrate": "128k", "pixel_format": "yuv420p", "movflags": "+faststart",
            },
        }, {"provider": "fixture", "model": "keep-all/1"})


class PinnedToolchainSmokeTests(unittest.TestCase):
    def test_full_standalone_pipeline(self) -> None:
        required = os.environ.get("VIDEO_EDIT_REQUIRE_PINNED_TOOLCHAIN") == "1"
        melt = os.environ.get("VIDEO_EDIT_PINNED_MELT")
        expected_version = os.environ.get("VIDEO_EDIT_PINNED_MELT_VERSION")
        ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
        if not melt or not ffmpeg or not ffprobe:
            if required:
                self.fail("required pinned toolchain variables/tools are unavailable")
            self.skipTest("set VIDEO_EDIT_PINNED_MELT to run the pinned render lane")
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory)
            source = root / "source.mp4"
            generated = run_checked([
                ffmpeg, "-v", "error", "-f", "lavfi", "-i", "testsrc2=duration=1:size=160x90:rate=30",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=1:sample_rate=48000",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", "-movflags", "+faststart", str(source),
            ])
            self.assertEqual(generated.returncode, 0)
            source_before = source.read_bytes()
            result = run_pipeline(
                source, "Keep the whole video and zoom in midway.", root / "job", model=ZoomModel(),
                ffmpeg=ffmpeg, ffprobe=ffprobe, melt=melt, max_analysis_frames=4,
                process_timeout=180, no_progress_timeout=60,
            )
            manifest = json.loads(result.manifest.read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "completed")
            self.assertEqual(manifest["render_validation"]["status"], "passed")
            self.assertTrue(manifest["render_validation"]["audio_present"])
            self.assertIn("inspection", manifest["artifacts"])
            self.assertTrue(result.edit_plan.is_file())
            self.assertTrue(result.project.is_file())
            self.assertTrue(result.output.is_file())
            self.assertEqual(source.read_bytes(), source_before)
            self.assertIn(manifest["accepted_render_id"], result.output.as_posix())
            inspection_path = inspect(
                result.output, result.edit_plan, result.workspace / "review",
                ffmpeg=ffmpeg, ffprobe=ffprobe,
            )
            inspection = json.loads(inspection_path.read_text(encoding="utf-8"))
            zoom_finding = next(item for item in inspection["automated_findings"] if item["keyframe_index"] == 1)
            self.assertEqual(zoom_finding["code"], "transform_applied")
            if expected_version:
                self.assertIn(expected_version, manifest["toolchain"]["melt"]["version"])


if __name__ == "__main__":
    unittest.main()
