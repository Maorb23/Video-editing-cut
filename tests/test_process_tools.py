from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from video_editing.inspect import _sample_frames
from video_editing.probe import probe_one
from video_editing.render import parse_progress, render


class ProcessToolTests(unittest.TestCase):
    def test_progress_parser(self) -> None:
        self.assertEqual(parse_progress("percentage: 37"), 37)
        self.assertEqual(parse_progress("progress = 105"), 100)
        self.assertIsNone(parse_progress("normal diagnostic"))

    def test_boundary_sampling_includes_neighbors(self) -> None:
        frames = {frame for frame, _ in _sample_frames(100, {50}, __import__("fractions").Fraction(25, 1))}
        self.assertTrue({49, 50, 51}.issubset(frames))

    @patch("video_editing.probe.run_checked")
    def test_probe_retains_vfr_rates_and_fingerprint(self, run_checked: MagicMock) -> None:
        with tempfile.TemporaryDirectory() as directory:
            media = Path(directory) / "silent video.mp4"
            media.write_bytes(b"abc")
            run_checked.return_value.stdout = json.dumps({
                "format": {"duration": "1.001"},
                "streams": [{"codec_type": "video", "codec_name": "h264", "width": 2, "height": 2, "avg_frame_rate": "24000/1001", "r_frame_rate": "30000/1001", "time_base": "1/90000"}],
            })
            result = probe_one(media, ffprobe="ffprobe")
            self.assertEqual(result["video"]["avg_frame_rate"], "24000/1001")
            self.assertEqual(result["duration_seconds"], "1001/1000")
            self.assertTrue(result["fingerprint"].startswith("sha256:"))

    @patch("video_editing.render.subprocess.Popen")
    def test_render_uses_list_arguments_and_atomic_partial(self, popen: MagicMock) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project.mlt"
            project.write_text("<mlt/>", encoding="utf-8")
            output = root / "final.mp4"
            process = MagicMock()
            process.stdout = iter(["percentage: 50\n", "percentage: 100\n"])
            process.wait.return_value = 0

            def create_partial(arguments, **kwargs):
                destination = next(value.removeprefix("avformat:") for value in arguments if value.startswith("avformat:"))
                Path(destination).write_bytes(b"mp4")
                return process

            popen.side_effect = create_partial
            stream = io.StringIO()
            render(project, output, melt="melt", progress_stream=stream)
            arguments = popen.call_args.args[0]
            self.assertIsInstance(arguments, list)
            self.assertIn("vcodec=libx264", arguments)
            self.assertEqual(output.read_bytes(), b"mp4")
            self.assertIn('"percent": 100', stream.getvalue())

    @patch("video_editing.render.subprocess.Popen")
    def test_interrupted_render_terminates_and_removes_partial(self, popen: MagicMock) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project.mlt"
            project.write_text("<mlt/>", encoding="utf-8")
            output = root / "final.mp4"
            partial = root / ".final.mp4.partial.mp4"
            partial.write_bytes(b"partial")
            process = MagicMock()
            process.stdout.__iter__.side_effect = KeyboardInterrupt
            popen.return_value = process
            with self.assertRaises(KeyboardInterrupt):
                render(project, output, melt="melt", progress_stream=io.StringIO())
            process.terminate.assert_called_once()
            self.assertFalse(partial.exists())


if __name__ == "__main__":
    unittest.main()
