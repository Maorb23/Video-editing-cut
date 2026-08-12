from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path
from unittest.mock import MagicMock, patch

from video_editing.inspect import (
    _compare_frames,
    _extract_frame,
    _sample_frame_reasons,
    _sample_frames,
    _transform_keyframes,
    evaluate_inspection,
    inspect,
)
from video_editing.mlt import write_mlt
from video_editing.plan import validate_plan
from video_editing.render import render

from tests.helpers import valid_plan


def _ppm(path: Path, value: int, *, width: int = 32, height: int = 24) -> None:
    pixel = bytes((value, 255 - value, (value * 3) % 256))
    path.write_bytes(f"P6\n{width} {height}\n255\n".encode("ascii") + pixel * width * height)


class InspectionUnitTests(unittest.TestCase):
    def test_keyframe_sampling_clips_neighbors_and_deduplicates_overlaps(self) -> None:
        reasons = _sample_frame_reasons(12, {0, 11}, {0, 1, 10, 11}, Fraction(30000, 1001))
        self.assertEqual(sorted(reasons), [0, 1, 2, 9, 10, 11])
        self.assertIn("keyframe", reasons[0])
        self.assertIn("boundary", reasons[0])
        self.assertEqual(len(reasons), len(set(reasons)))

    def test_regular_sampling_preserves_rational_rate(self) -> None:
        frames = [frame for frame, _ in _sample_frames(100, set(), Fraction(30000, 1001))]
        self.assertEqual(frames, [0, 30, 60, 90, 99])

    def test_keyframes_include_clip_and_nonzero_operation_starts(self) -> None:
        plan = {
            "tracks": [{"clips": [{"id": "clip", "asset_id": "asset", "timeline_start": 100, "source_in": 7}]}],
            "operations": [{
                "id": "move", "type": "transform", "target": "clip", "start": 20,
                "keyframes": [{"frame": 5, "geometry": "10%/0%:90%x90%"}],
            }],
        }
        coverage = _transform_keyframes(plan, 200)
        self.assertEqual(coverage[0]["timeline_frame"], 125)
        self.assertEqual(coverage[0]["source_frame"], 32)
        self.assertEqual(coverage[0]["required_frames"], [124, 125, 126])

    @patch("video_editing.inspect.run_checked")
    def test_exact_extraction_selects_decoded_frame_number(self, run_checked: MagicMock) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "frame.png"

            def create(arguments: list[str]):
                output.write_bytes(b"png")
                return MagicMock(stdout="", stderr="")

            run_checked.side_effect = create
            _extract_frame(Path("video.mp4"), 37, output, "ffmpeg")
            arguments = run_checked.call_args.args[0]
            self.assertIn("select=eq(n\\,37)", arguments)
            self.assertNotIn("-ss", arguments)

    def test_incomplete_or_failed_evidence_cannot_pass(self) -> None:
        record = {
            "status": "passed",
            "frames": [{"frame": 10, "review": {"status": "pass"}}],
            "audio": {"review": {"status": "pass"}},
            "keyframe_coverage": [{
                "operation_id": "move", "required_frames": [9, 10, 11],
                "sampled_frames": [9, 10], "status": "missing",
            }],
            "automated_findings": [{"code": "effect_not_applied", "status": "fail"}],
        }
        evaluation = evaluate_inspection(record)
        self.assertEqual(evaluation["status"], "failed")
        self.assertFalse(evaluation["eligible"])
        self.assertEqual({item["code"] for item in evaluation["blockers"]}, {"missing_keyframe_evidence", "effect_not_applied"})

    @patch("video_editing.inspect.run_checked")
    def test_frame_comparison_parses_ssim(self, run_checked: MagicMock) -> None:
        run_checked.return_value.stderr = "[Parsed_ssim_0] SSIM Y:1.0 All:0.998765 (29.0)"
        self.assertEqual(_compare_frames(Path("render.png"), Path("source.png"), "ffmpeg"), 0.998765)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg and ffprobe are unavailable")
class InspectionFfmpegTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.ffmpeg = shutil.which("ffmpeg") or "ffmpeg"
        self.ffprobe = shutil.which("ffprobe") or "ffprobe"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _numbered_video(self, frame_count: int = 8) -> Path:
        for frame in range(frame_count):
            _ppm(self.root / f"numbered-{frame:02d}.ppm", 20 + frame * 20)
        output = self.root / "numbered.mkv"
        subprocess.run([
            self.ffmpeg, "-v", "error", "-framerate", "30000/1001",
            "-i", str(self.root / "numbered-%02d.ppm"), "-c:v", "ffv1", str(output),
        ], check=True)
        return output

    def test_exact_extraction_matches_requested_numbered_frame(self) -> None:
        video = self._numbered_video()
        extracted = self.root / "frame-00000005.png"
        _extract_frame(video, 5, extracted, self.ffmpeg)
        self.assertEqual(_compare_frames(extracted, self.root / "numbered-05.ppm", self.ffmpeg), 1.0)

    def test_ignored_transform_is_an_automated_failure(self) -> None:
        video = self._numbered_video()
        plan = valid_plan(video)
        plan["profile"].update({"width": 32, "height": 24})
        plan["assets"][0]["duration_frames"] = 8
        plan["tracks"][0]["clips"][0]["duration"] = 8
        plan["operations"] = [{
            "id": "move", "type": "transform", "target": "c1", "start": 0, "duration": 8,
            "keyframes": [{"frame": 3, "geometry": "10%/10%:80%x80%"}],
        }]
        plan_path = self.root / "edit-plan.json"
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
        inspection_path = inspect(video, plan_path, self.root / "review", ffmpeg=self.ffmpeg, ffprobe=self.ffprobe)
        record = json.loads(inspection_path.read_text(encoding="utf-8"))
        self.assertIn("effect_not_applied", {item["code"] for item in record["automated_findings"]})
        self.assertEqual(record["status"], "failed")


MELT = shutil.which("melt") or shutil.which("melt-7") or shutil.which("melt.exe")


@unittest.skipUnless(MELT and shutil.which("ffmpeg") and shutil.which("ffprobe"), "Melt integration tools are unavailable")
class TransformRenderRegressionTests(unittest.TestCase):
    def test_mlt_applies_transform_animation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ffmpeg = shutil.which("ffmpeg") or "ffmpeg"
            source = root / "source.mkv"
            subprocess.run([
                ffmpeg, "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=64x48:rate=10:duration=1.2",
                "-c:v", "ffv1", str(source),
            ], check=True)
            plan = valid_plan(source)
            plan["profile"].update({"width": 64, "height": 48, "frame_rate": {"numerator": 10, "denominator": 1}})
            plan["assets"][0]["duration_frames"] = 12
            plan["tracks"][0]["clips"][0]["duration"] = 12
            plan["operations"] = [{
                "id": "move", "type": "transform", "target": "c1", "duration": 12,
                "keyframes": [
                    {"frame": 0, "geometry": "0%/0%:100%x100%"},
                    {"frame": 6, "geometry": "20%/10%:60%x60%"},
                ],
            }]
            plan_path = root / "edit-plan.json"
            plan_path.write_text(json.dumps(plan), encoding="utf-8")
            project = root / "project.mlt"
            write_mlt(validate_plan(plan, source=plan_path), project)
            rendered = root / "rendered.mp4"
            render(project, rendered, melt=MELT)
            inspection_path = inspect(rendered, plan_path, root / "review")
            record = json.loads(inspection_path.read_text(encoding="utf-8"))
            finding = next(item for item in record["automated_findings"] if item["keyframe_index"] == 1)
            self.assertEqual(finding["code"], "transform_applied")


if __name__ == "__main__":
    unittest.main()
