from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tests.helpers import valid_plan
from video_editing.mlt import compile_mlt
from video_editing.plan import validate_plan
from video_editing.probe import fingerprint
from video_editing.silence import apply_silence_removal


class CuratedEffectsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.media = self.root / "source.mp4"
        self.media.write_bytes(b"immutable source")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_silence_removal_shortens_linked_audio_and_video_in_sync(self) -> None:
        plan = valid_plan(self.media)
        plan["tracks"][0]["clips"][0]["duration"] = 300
        plan["tracks"].append({"id": "a1", "kind": "audio", "clips": [
            {"id": "audio", "asset_id": "a", "timeline_start": 0, "source_in": 0, "duration": 300},
        ]})
        evidence = {"intervals": [{"start_frame": 100, "end_frame": 200}]}
        edited = apply_silence_removal(plan, asset_id="a", evidence=evidence,
                                       padding_seconds=0, crossfade_seconds=0.04)
        validated = validate_plan(edited, source=self.root / "plan.json")
        video, audio = validated.resolved_tracks
        self.assertEqual(sum(c["duration"] for c in video["clips"]), 200)
        self.assertEqual([(c["timeline_start"], c["source_in"], c["duration"]) for c in video["clips"]],
                         [(c["timeline_start"], c["source_in"], c["duration"]) for c in audio["clips"]])
        self.assertEqual(self.media.read_bytes(), b"immutable source")
        self.assertEqual(len([op for op in edited["operations"] if op["type"] == "fade_audio"]), 4)

    def test_animated_color_and_mask_compile_to_curated_filter(self) -> None:
        mask = self.root / "mask.png"
        mask.write_bytes(b"mask")
        plan = valid_plan(self.media)
        plan["operations"] = [{
            "id": "grade", "type": "color_grade", "target": "c1", "duration": 100,
            "keyframes": [{"frame": 0, "tint": "#ff0000", "saturation": 0.5},
                          {"frame": 99, "tint": "#0000ff", "saturation": 1.5}],
            "mask": {"resource": "mask.png", "softness": 0.1, "invert": False},
        }]
        root = compile_mlt(validate_plan(plan, source=self.root / "plan.json"), self.root / "out.mlt").getroot()
        node = root.find("./producer/filter[@id='ves_filter_grade']")
        props = {p.get("name"): p.text for p in node.findall("property")}
        self.assertEqual(props["mlt_service"], "avfilter.colorbalance")
        self.assertEqual(props["av.tint"], "0=#ff0000;99=#0000ff")
        self.assertEqual(props["av.mask"], "mask.png")

    def test_eq_reverb_and_dereverb_use_valid_synchronized_audio(self) -> None:
        derived = self.root / "dereverbed.wav"
        derived.write_bytes(b"derived audio")
        plan = valid_plan(self.media)
        plan["assets"].append({"id": "clean", "path": derived.name, "kind": "audio", "duration_frames": 300,
                               "fingerprint": fingerprint(derived), "probe": {}})
        plan["operations"] = [
            {"id": "eq", "type": "parametric_eq", "target": "c1",
             "bands": [{"frequency": 1000, "gain_db": 6, "q": 1.2}]},
            {"id": "verb", "type": "reverb", "target": "c1", "wet": .25, "dry": 1.0},
            {"id": "clean", "type": "dereverb", "target": "c1", "derived_asset_id": "clean",
             "model": "deepfilternet3-local", "model_sha256": "sha256:" + "1" * 64},
        ]
        root = compile_mlt(validate_plan(plan, source=self.root / "plan.json"), self.root / "out.mlt").getroot()
        producer = next(p for p in root.findall("producer") if p.get("id", "").startswith("ves_producer_"))
        props = {p.get("name"): p.text for p in producer.findall("property")}
        self.assertEqual(props["resource"], "source.mp4")
        self.assertEqual(props["audio_index"], "-1")
        producer = next(p for p in root.findall("producer")
                        if p.find("./property[@name='resource']").text == "dereverbed.wav")
        props = {p.get("name"): p.text for p in producer.findall("property")}
        self.assertEqual(props["video-editing-skill:asset-id"], "clean")
        self.assertEqual(props["video-editing-skill:fingerprint"], fingerprint(derived))
        services = [n.find("./property[@name='mlt_service']").text for n in producer.findall("filter")]
        self.assertEqual(services, ["avfilter.equalizer", "avfilter.aecho"])


if __name__ == "__main__":
    unittest.main()
