from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from tests.helpers import valid_plan
from tests.test_planning import FakeModel, draft
from tests import test_planning as planning_fixtures
from video_editing.audio import create_dereverberated_asset, dereverb_preflight, wants_dereverb
from video_editing.dereverb_pin import VERSION
from video_editing.errors import VideoEditingError
from video_editing.mlt import compile_mlt
from video_editing.plan import validate_plan
from video_editing.planning import EditPlanner
from video_editing.probe import fingerprint


def wav(path: Path, samples: int = 48000, *, rate: int = 48000, channels: int = 1, value: int = 200) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as stream:
        stream.setparams((channels, 2, rate, samples, "NONE", "not compressed"))
        stream.writeframes(value.to_bytes(2, "little", signed=True) * samples * channels)


class DereverbTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source.wav"
        wav(self.source)
        self.model = self.root / "model.tar.gz"
        self.model.write_bytes(b"fixture model")
        self.executable = self.root / ("deep-filter.exe" if os.name == "nt" else "deep-filter")
        self.executable.write_bytes(b"fixture executable")
        self.executable.chmod(0o755)
        self.options = {"model_path": self.model, "model_sha256": fingerprint(self.model),
                        "executable": str(self.executable), "executable_sha256": fingerprint(self.executable)}

    def runner(self, args, **kwargs):
        if args[-1] == "--version":
            return subprocess.CompletedProcess(args, 0, VERSION, "")
        if args[0] == str(self.executable):
            output = Path(args[args.index("--output-dir") + 1]) / "input.wav"
            wav(output, 48480, value=100)
        else:
            output = Path(args[-1])
            if output.name == "source.wav":
                shutil.copyfile(self.source, output)
            elif output.name == "input.wav":
                wav(output, 49920)
            elif output.name == "checked.wav":
                shutil.copyfile(Path(args[args.index("-i") + 1]), output)
            else:
                wav(output, value=100)
        return subprocess.CompletedProcess(args, 0, "", "")

    def create(self, **extra):
        return create_dereverberated_asset(self.source, self.root / "derived" / "audio.wav", **self.options, **extra)

    def test_model_fingerprint_mismatch_precedes_process(self):
        self.model.write_bytes(b"tampered")
        with patch("video_editing.audio.run_checked") as run:
            with self.assertRaises(VideoEditingError) as caught:
                self.create()
        self.assertEqual(caught.exception.code, "fingerprint_mismatch")
        run.assert_not_called()
        self.assertFalse((self.root / "derived").exists())

    def test_missing_model_or_binary_is_unavailable(self):
        for key, value in (("model_path", self.root / "missing"), ("executable", str(self.root / "missing"))):
            with self.subTest(key=key), self.assertRaises(VideoEditingError) as caught:
                dereverb_preflight(**{**self.options, key: value})
            self.assertEqual(caught.exception.code, "dereverb_unavailable")

    def test_atomic_publish_and_immutable_source(self):
        before = self.source.read_bytes()
        def run(args, **kwargs):
            self.assertFalse((self.root / "derived").exists())
            return self.runner(args, **kwargs)
        with patch("video_editing.audio.run_checked", side_effect=run):
            output = self.create()
        manifest = json.loads(output.with_name("manifest.json").read_text())
        self.assertEqual(manifest["output_sha256"], fingerprint(output))
        self.assertEqual(manifest["input_sha256"], fingerprint(self.source))
        self.assertEqual(manifest["audio"]["samples"], 48000)
        self.assertEqual(self.source.read_bytes(), before)
        self.assertFalse(list(self.root.glob(".dereverb-*")))
        with patch("video_editing.audio.run_checked", side_effect=self.runner):
            with self.assertRaises(VideoEditingError) as caught:
                self.create()
        self.assertEqual(caught.exception.code, "output_exists")
        # Windows temporary-directory cleanup must clear readonly outputs.
        for path in output.parent.iterdir():
            path.chmod(0o600)

    def test_invalid_and_wrong_metadata_leave_no_partial_asset(self):
        for mode in ("missing", "empty", "short", "channels", "rate", "timeout"):
            def run(args, **kwargs):
                if args[0] == str(self.executable) and "--model" in args:
                    output = Path(args[args.index("--output-dir") + 1]) / "input.wav"
                    if mode == "timeout":
                        raise VideoEditingError("timeout", code="process_timeout")
                    if mode != "missing":
                        wav(output, 0 if mode == "empty" else 400 if mode == "short" else 48480,
                            channels=2 if mode == "channels" else 1, rate=44100 if mode == "rate" else 48000)
                    return subprocess.CompletedProcess(args, 0, "", "")
                return self.runner(args, **kwargs)
            with self.subTest(mode=mode), patch("video_editing.audio.run_checked", side_effect=run):
                with self.assertRaises(VideoEditingError):
                    self.create()
            self.assertFalse((self.root / "derived").exists())
            self.assertFalse(list(self.root.glob(".dereverb-*")))

    def test_intent_and_unavailable_planning(self):
        for text in ("remove echo", "Remove room reverb", "dereverberate", "remove the room echo"):
            self.assertTrue(wants_dereverb(text))
        for text in ("make the audio louder", "add room reverb", "do not remove echo"):
            self.assertFalse(wants_dereverb(text))
        model = FakeModel([])
        with self.assertRaises(VideoEditingError) as caught:
            EditPlanner(model).plan("remove echo", planning_fixtures.PlanningTests().analysis(self.root, self.source),
                                   plan_path=self.root / "plan.json", source_relative=self.source.name)
        self.assertEqual(caught.exception.code, "dereverb_unavailable")
        self.assertEqual(model.calls, [])

    def test_unchanged_output_is_a_hard_failure(self):
        def run(args, **kwargs):
            result = self.runner(args, **kwargs)
            if Path(args[-1]).name == "audio.wav":
                wav(Path(args[-1]), value=200)
            return result
        with patch("video_editing.audio.run_checked", side_effect=run), self.assertRaises(VideoEditingError) as caught:
            self.create()
        self.assertEqual(caught.exception.code, "dereverb_unchanged")
        self.assertFalse((self.root / "derived").exists())

    def test_ordinary_audio_request_does_not_add_cleaning(self):
        model = FakeModel([draft(operations=[{"id": "volume", "type": "volume", "target": "c1", "gain_db": 3}])])
        planned = EditPlanner(model).plan("Make audio louder", planning_fixtures.PlanningTests().analysis(self.root, self.source),
                                         plan_path=self.root / "plan.json", source_relative=self.source.name)
        self.assertEqual([op["type"] for op in planned.plan.data["operations"]], ["volume"])

    def test_planning_assembles_preflight_and_keeps_prior_operation(self):
        with patch("video_editing.audio.run_checked", side_effect=self.runner):
            output = self.create()
        analysis = planning_fixtures.PlanningTests().analysis(self.root, self.source)
        analysis.data["source"]["audio"] = {"sample_rate": 48000, "channels": 1}
        manifest = json.loads(output.with_name("manifest.json").read_text())
        previous = {"operations": [{"id": "volume", "type": "volume", "target": "c1", "gain_db": 3}]}
        previous["tracks"] = draft(duration=15)["tracks"]
        model = FakeModel([draft()])
        planned = EditPlanner(model).plan("remove echo", analysis, plan_path=self.root / "plan.json",
            source_relative=self.source.name, previous_plan=previous,
            dereverb={"asset_path": "derived/audio.wav", "manifest": manifest})
        self.assertEqual([op["type"] for op in planned.plan.data["operations"]], ["volume", "dereverb"])
        self.assertEqual(planned.plan.data["tracks"][0]["clips"][0]["duration"], 15)
        self.assertNotIn("derived/audio.wav", model.calls[0]["input_text"])
        changed = planned.plan.data
        changed["analysis"]["dereverb"]["model_sha256"] = "sha256:" + "f" * 64
        with self.assertRaises(VideoEditingError) as caught:
            validate_plan(changed, source=self.root / "plan.json")
        self.assertIn("fingerprint_mismatch", {issue.code for issue in caught.exception.issues})
        output.chmod(0o600)
        output.with_name("manifest.json").chmod(0o600)

    def test_compiler_retains_video_and_aligns_split_trimmed_audio(self):
        derived = self.root / "clean.wav"
        wav(derived)
        plan = valid_plan(self.source)
        plan["assets"].append({"id": "clean", "path": "clean.wav", "kind": "audio", "duration_frames": 300,
                               "fingerprint": fingerprint(derived), "probe": {}})
        plan["operations"] = [
            {"id": "trim", "type": "trim", "target": "c1", "source_in": 30, "duration": 60},
            {"id": "split", "type": "split", "target": "c1", "at": 30},
            {"id": "clean", "type": "dereverb", "target": "c1", "derived_asset_id": "clean",
             "model": "deepfilternet3-local", "model_sha256": fingerprint(self.model)},
        ]
        root = compile_mlt(validate_plan(plan, source=self.root / "plan.json"), self.root / "project.mlt").getroot()
        video, audio = [p for p in root.findall("playlist") if p.get("id").startswith("ves_playlist")]
        self.assertEqual([(e.get("in"), e.get("out")) for e in video.findall("entry")],
                         [(e.get("in"), e.get("out")) for e in audio.findall("entry")])
        muted = root.findall("./producer/property[@name='audio_index']")
        self.assertEqual([p.text for p in muted], ["-1", "-1"])
        self.assertEqual(len(audio.findall("entry")), 2)
        resources = [p.text for p in root.findall("./producer/property[@name='resource']")]
        self.assertEqual(resources.count(self.source.name), 2)
        self.assertEqual(resources.count("clean.wav"), 2)


if __name__ == "__main__":
    unittest.main()
