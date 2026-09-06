"""Real FFmpeg/MLT flow with a hash-pinned fake CPU executable and model."""
from __future__ import annotations

import functools
import array
import json
import math
import os
import shutil
import tempfile
import unittest
import wave
from fractions import Fraction
from pathlib import Path
from unittest.mock import patch

from tests.test_planning import draft
from video_editing.audio import create_dereverberated_asset, dereverb_preflight, wav_metadata
from video_editing.dereverb_pin import MODEL_SHA256, VERSION
from video_editing.errors import VideoEditingError
from video_editing.pipeline import run_pipeline
from video_editing.planning import ModelResponse
from video_editing.probe import fingerprint
from video_editing.process import run_checked


class KeepModel:
    def generate(self, **kwargs):
        context = json.loads(kwargs["input_text"])
        return ModelResponse(draft(duration=context["analysis"]["source"]["duration_frames"]), {"provider": "fixture"})


@unittest.skipUnless(os.name == "posix" and shutil.which("ffmpeg") and shutil.which("melt"), "requires Linux worker toolchain")
class DereverbPipelineTests(unittest.TestCase):
    def test_audio_stream_offset_is_preserved_and_execution_is_bounded(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "offset.mkv"
            run_checked(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=duration=1:size=160x90:rate=30",
                "-itsoffset", "0.25", "-f", "lavfi", "-i", "sine=duration=0.7:sample_rate=48000",
                "-c:v", "libx264", "-c:a", "pcm_s16le", str(source)])
            executable = root / "deep-filter"
            shutil.copyfile(Path(__file__).parents[1] / "fixtures" / "fake_dereverb.py", executable)
            executable.chmod(0o755)
            model = root / "model"
            for mode in ("noisy", "timeout"):
                model.write_text(mode, encoding="utf-8")
                options = {"executable": str(executable), "executable_sha256": fingerprint(executable),
                           "model_path": model, "model_sha256": fingerprint(model), "max_diagnostic_bytes": 4096}
                output = root / mode / "audio.wav"
                if mode == "timeout":
                    with self.assertRaises(VideoEditingError) as caught:
                        create_dereverberated_asset(source, output, timeout=0.5, **options)
                    self.assertEqual(caught.exception.code, "process_timeout")
                    self.assertFalse(output.parent.exists())
                else:
                    create_dereverberated_asset(source, output, timeout=30, **options)
                    with wave.open(str(output), "rb") as reader:
                        leading = array.array("h", reader.readframes(10000))
                        speech = array.array("h", reader.readframes(20000))
                    self.assertEqual(max(abs(sample) for sample in leading), 0)
                    self.assertGreater(max(abs(sample) for sample in speech), 100)

    @unittest.skipUnless(Path("/opt/dereverb/model.tar.gz").is_file(), "requires bundled production model")
    def test_real_bundled_cpu_model(self):
        state = dereverb_preflight()
        self.assertEqual(state["model_sha256"], MODEL_SHA256)
        self.assertEqual(state["executable_version"], VERSION)
        from service import worker
        with patch.object(worker.Settings, "from_env"), patch.object(worker, "PostgresRepository"), \
                patch.object(worker, "build_storage"), patch.object(worker, "Worker"), patch.object(worker, "emit") as emit:
            self.assertEqual(worker.main(["--once"]), 0)
            emit.assert_called_once_with("dereverb_ready", model="deepfilternet3-local", model_sha256=MODEL_SHA256,
                                         executable_version=VERSION, device="cpu")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.wav"
            run_checked(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=duration=1:sample_rate=44100",
                         "-ac", "2", "-c:a", "pcm_s16le", str(source)])
            before = fingerprint(source)
            output = create_dereverberated_asset(source, root / "derived" / "audio.wav", timeout=60)
            self.assertEqual(wav_metadata(output), wav_metadata(source))
            self.assertEqual(fingerprint(source), before)
            manifest = json.loads(output.with_name("manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["model_sha256"], MODEL_SHA256)
            self.assertEqual(manifest["device"], "cpu")

    def test_full_flow_and_failed_cleaning_cannot_accept_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.mp4"
            run_checked(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=duration=1.2:size=160x90:rate=30000/1001",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=1.2:sample_rate=44100",
                "-c:v", "libx264", "-c:a", "aac", "-ac", "2", "-shortest", str(source)])
            original_hash = fingerprint(source)
            executable = root / "deep-filter"
            shutil.copyfile(Path(__file__).parents[1] / "fixtures" / "fake_dereverb.py", executable)
            executable.chmod(0o755)
            model = root / "model"
            for mode in ("clean", "missing"):
                model.write_text(mode, encoding="utf-8")
                create = functools.partial(create_dereverberated_asset, executable=str(executable),
                    executable_sha256=fingerprint(executable), model_path=model, model_sha256=fingerprint(model))
                job = root / mode
                with patch("video_editing.audio.create_dereverberated_asset", side_effect=create):
                    if mode == "missing":
                        with self.assertRaises(VideoEditingError) as caught:
                            run_pipeline(source, "Remove echo", job, model=KeepModel(), frame_rate=Fraction(30000, 1001), max_analysis_frames=2)
                        self.assertEqual(caught.exception.code, "derived_asset_missing")
                        self.assertFalse((job / "result.json").exists())
                        self.assertFalse(list((job / "renders").rglob("*.mp4")))
                        self.assertFalse((job / "derived" / "dereverb").exists())
                        continue
                    result = run_pipeline(source, "Remove room echo", job, model=KeepModel(), frame_rate=Fraction(30000, 1001), max_analysis_frames=2)
                manifest = json.loads(result.manifest.read_text(encoding="utf-8"))
                self.assertEqual(manifest["status"], "completed")
                self.assertEqual(manifest["render_validation"]["status"], "passed")
                self.assertEqual(manifest["timeline_policy"]["frame_rate"], {"numerator": 30000, "denominator": 1001})
                audio = wav_metadata(job / "derived" / "dereverb" / "audio.wav")
                self.assertEqual((audio["sample_rate"], audio["channels"]), (44100, 2))
                self.assertEqual(fingerprint(source), original_hash)
                probe = json.loads(run_checked(["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(result.output)]).stdout)
                video, rendered_audio = probe["streams"]
                self.assertEqual(video["avg_frame_rate"], "30000/1001")
                self.assertEqual(int(rendered_audio["sample_rate"]), 44100)
                self.assertEqual(rendered_audio["channels"], 2)
                # The fake model halves PCM amplitude. This catches an unmuted
                # embedded stream or duplicate derived track in an actual render.
                levels = []
                for index, media in enumerate((source, result.output)):
                    decoded = root / f"level-{index}.wav"
                    run_checked(["ffmpeg", "-v", "error", "-i", str(media), "-ss", "0.2", "-t", "0.6",
                                 "-vn", "-c:a", "pcm_s16le", str(decoded)])
                    with wave.open(str(decoded), "rb") as reader:
                        samples = array.array("h", reader.readframes(reader.getnframes()))
                    levels.append(math.sqrt(sum(sample * sample for sample in samples) / len(samples)))
                self.assertAlmostEqual(levels[1] / levels[0], 0.5, delta=0.035)


if __name__ == "__main__":
    unittest.main()
