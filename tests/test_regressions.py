from __future__ import annotations

import json
import shutil
import tempfile
import unittest
import xml.etree.ElementTree as ET
from fractions import Fraction
from pathlib import Path

from video_editing.analysis import FrameAnalysisProvider, project_duration_frames, sample_project_frames
from video_editing.artifacts import validate_compiled_mlt, validate_rendered_video
from video_editing.errors import PlanValidationError, VideoEditingError
from video_editing.mlt import write_mlt
from video_editing.plan import validate_plan
from video_editing.process import run_checked
from video_editing.supervisor import ProcessLimits, ProcessSupervisor, Toolchain
from video_editing.workspace import JobWorkspace

from tests.helpers import valid_plan


FIXTURES = Path(__file__).parent / "fixtures" / "regressions"


class PhaseZeroRegressionTests(unittest.TestCase):
    def fixture(self, name: str) -> dict:
        return json.loads((FIXTURES / name).read_text(encoding="utf-8"))

    def test_blank_host_path_is_rejected(self) -> None:
        fixture = self.fixture("blank-path.json")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            asset = root / "source.mp4"
            asset.write_bytes(b"media")
            plan = valid_plan(asset)
            plan["assets"][0]["path"] = fixture["source_path"]
            with self.assertRaises(PlanValidationError) as context:
                validate_plan(plan, source=root / "edit-plan.json", check_files=False, require_confined_paths=True)
            self.assertIn(fixture["expected_error"], {issue.code for issue in context.exception.issues})

    def test_missing_transform_is_rejected_after_compilation(self) -> None:
        fixture = self.fixture("missing-transform.json")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            asset = root / "source.mp4"
            asset.write_bytes(b"media")
            plan_data = valid_plan(asset)
            plan_data["operations"] = [fixture["operation"]]
            plan = validate_plan(plan_data, source=root / "edit-plan.json", require_confined_paths=True)
            project = root / "project.mlt"
            write_mlt(plan, project)
            tree = ET.parse(project)
            producer = next(item for item in tree.getroot().findall("producer") if item.find("./filter[@id='ves_filter_zoom']") is not None)
            producer.remove(producer.find("./filter[@id='ves_filter_zoom']"))
            tree.write(project, encoding="utf-8", xml_declaration=True)
            with self.assertRaises(VideoEditingError) as context:
                validate_compiled_mlt(project, plan, root)
            self.assertEqual(context.exception.code, fixture["expected_error"])

    def test_corrupt_output_never_becomes_accepted(self) -> None:
        ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
        if not ffmpeg or not ffprobe:
            self.skipTest("FFmpeg tools unavailable")
        fixture = self.fixture("corrupt-output.json")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            asset = root / "source.mp4"
            asset.write_bytes(b"media")
            plan = validate_plan(valid_plan(asset), source=root / "edit-plan.json")
            corrupt = root / "output.mp4"
            corrupt.write_bytes(fixture["payload"].encode("utf-8"))
            tools = Toolchain(Path(ffmpeg).resolve(), Path(ffprobe).resolve(), Path(ffmpeg).resolve())
            supervisor = ProcessSupervisor(ProcessLimits(wall_timeout=10, no_progress_timeout=5))
            with self.assertRaises(VideoEditingError) as context:
                validate_rendered_video(corrupt, plan, tools, supervisor, root)
            self.assertEqual(context.exception.code, fixture["expected_error"])

    def test_vfr_analysis_uses_bounded_cfr_project_frames(self) -> None:
        fixture = self.fixture("vfr-642.json")
        rate = Fraction(fixture["project_frame_rate"])
        duration = project_duration_frames(fixture["duration_seconds"], rate)
        self.assertEqual(duration, fixture["expected_project_frames"])
        samples = sample_project_frames(duration, 12)
        self.assertLessEqual(max(samples), duration - 1)
        self.assertGreater(fixture["failed_source_frame"], fixture["source_decoded_frames"] - 1)
        self.assertEqual(project_duration_frames("51/50", Fraction(30, 1)), 31)
        with self.assertRaises(ValueError):
            FrameAnalysisProvider(max_samples=FrameAnalysisProvider.MAX_SAMPLES + 1)

    def test_vfr_input_is_sampled_from_its_cfr_proxy(self) -> None:
        ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
        if not ffmpeg or not ffprobe:
            self.skipTest("FFmpeg tools unavailable")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "variable-rate.mp4"
            run_checked([
                ffmpeg, "-v", "error",
                "-f", "lavfi", "-i", "testsrc2=duration=0.5:size=64x64:rate=10",
                "-f", "lavfi", "-i", "testsrc2=duration=0.5:size=64x64:rate=30",
                "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0", "-fps_mode", "vfr",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", str(source),
            ])
            workspace = JobWorkspace.create(root / "job")
            copied = workspace.import_media(source)
            tools = Toolchain(Path(ffmpeg).resolve(), Path(ffprobe).resolve(), Path(ffmpeg).resolve())
            analysis = FrameAnalysisProvider(frame_rate=Fraction(30, 1), max_samples=6).analyze(
                copied, workspace, tools, ProcessSupervisor(ProcessLimits(wall_timeout=20, no_progress_timeout=5)),
            )
            observed = [item["project_frame"] for item in analysis.data["observations"]]
            self.assertEqual(len(observed), 6)
            self.assertLessEqual(max(observed), analysis.data["source"]["duration_frames"] - 1)
            self.assertTrue(analysis.data["timeline_policy"]["source_is_vfr"])

    def test_analysis_bounds_samples_to_decodable_proxy_when_container_is_longer(self) -> None:
        ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
        if not ffmpeg or not ffprobe:
            self.skipTest("FFmpeg tools unavailable")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "longer-container.mp4"
            run_checked([
                ffmpeg, "-v", "error",
                "-f", "lavfi", "-i", "testsrc2=duration=0.4:size=64x64:rate=25",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=0.6:sample_rate=48000",
                "-map", "0:v:0", "-map", "1:a:0", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                "-c:a", "aac", str(source),
            ])
            source_probe = json.loads(run_checked([
                ffprobe, "-v", "error", "-show_entries", "format=duration:stream=codec_type,duration",
                "-of", "json", str(source),
            ]).stdout)
            video_stream = next(stream for stream in source_probe["streams"] if stream["codec_type"] == "video")
            self.assertGreater(Fraction(source_probe["format"]["duration"]), Fraction(video_stream["duration"]))

            workspace = JobWorkspace.create(root / "job")
            copied = workspace.import_media(source)
            tools = Toolchain(Path(ffmpeg).resolve(), Path(ffprobe).resolve(), Path(ffmpeg).resolve())
            rate = Fraction(30000, 1001)
            analysis = FrameAnalysisProvider(frame_rate=rate, max_samples=12).analyze(
                copied, workspace, tools, ProcessSupervisor(ProcessLimits(wall_timeout=20, no_progress_timeout=5)),
            )
            proxy = workspace.root / analysis.data["proxy"]
            proxy_probe = json.loads(run_checked([
                ffprobe, "-v", "error", "-select_streams", "v:0", "-count_frames",
                "-show_entries", "stream=nb_read_frames", "-of", "json", str(proxy),
            ]).stdout)
            decoded_frames = int(proxy_probe["streams"][0]["nb_read_frames"])
            observed = [item["project_frame"] for item in analysis.data["observations"]]

            self.assertEqual(analysis.data["timeline_policy"]["frame_rate"], {"numerator": 30000, "denominator": 1001})
            self.assertEqual(analysis.data["timeline_policy"]["duration_frames"], decoded_frames)
            self.assertEqual(analysis.data["source"]["duration_frames"], decoded_frames)
            self.assertEqual(observed[-1], decoded_frames - 1)
            self.assertEqual(len(analysis.frame_paths), len(observed))

    def test_entirely_black_output_is_rejected(self) -> None:
        ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
        if not ffmpeg or not ffprobe:
            self.skipTest("FFmpeg tools unavailable")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            asset = root / "source.mp4"
            asset.write_bytes(b"media")
            plan_data = valid_plan(asset)
            plan_data["profile"].update({"width": 16, "height": 16, "frame_rate": {"numerator": 30, "denominator": 1}})
            plan_data["assets"][0]["duration_frames"] = 3
            plan_data["tracks"][0]["clips"][0]["duration"] = 3
            plan = validate_plan(plan_data, source=root / "edit-plan.json")
            output = root / "black.mp4"
            generated = shutil.which("ffmpeg")
            assert generated is not None
            from video_editing.process import run_checked
            run_checked([
                generated, "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=16x16:r=30:d=0.1",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", str(output),
            ])
            tools = Toolchain(Path(ffmpeg).resolve(), Path(ffprobe).resolve(), Path(ffmpeg).resolve())
            with self.assertRaises(VideoEditingError) as context:
                validate_rendered_video(output, plan, tools, ProcessSupervisor(ProcessLimits(wall_timeout=10, no_progress_timeout=5)), root)
            self.assertEqual(context.exception.code, "render_validation_failed")


if __name__ == "__main__":
    unittest.main()
