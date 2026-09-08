from __future__ import annotations

import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from video_editing.errors import VideoEditingError
from video_editing.mlt import compile_mlt, write_mlt
from video_editing.plan import ValidatedPlan, validate_plan

from tests.helpers import valid_plan


class MltTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.asset = self.root / "clip's & ü.mp4"
        self.asset.write_bytes(b"immutable media")
        self.before = self.asset.read_bytes()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def validated(self):
        plan = valid_plan(self.asset)
        plan["operations"] = [
            {"id": "caption", "type": "caption", "target": "c1", "start": 3, "duration": 10, "text": "Tom & Jerry <3"},
            {"id": "filter", "type": "filter", "target": "c1", "name": "brightness", "properties": {"level": 1.1}},
        ]
        return validate_plan(plan, source=self.root / "plan.json")

    def test_compilation_is_deterministic_and_xml_escaped(self) -> None:
        plan = self.validated()
        compiled = compile_mlt(plan, self.root / "one.mlt").getroot()
        one = ET.tostring(compiled, encoding="unicode")
        two = ET.tostring(compile_mlt(plan, self.root / "one.mlt").getroot(), encoding="unicode")
        self.assertEqual(one, two)
        self.assertIn("Tom &amp; Jerry &lt;3", one)
        self.assertIn("30000", one)
        self.assertNotIn("root=", one)
        media_producer = next(child for child in compiled.findall("producer") if child.get("id", "").startswith("ves_producer_"))
        self.assertEqual(media_producer.get("in"), "00:00:00.000000")
        self.assertEqual(media_producer.get("out"), "00:00:09.976633")
        children = list(compiled)
        first_playlist = next(index for index, child in enumerate(children) if child.tag == "playlist")
        last_producer = max(index for index, child in enumerate(children) if child.tag == "producer")
        self.assertLess(last_producer, first_playlist)
        self.assertFalse(compiled.findall("property"), "MLT root properties have no producer parent")
        export_properties = {
            item.get("name"): item.text
            for item in compiled.findall("./tractor[@id='ves_main']/property")
            if item.get("name", "").startswith("video-editing-skill:export.")
        }
        self.assertEqual(export_properties["video-editing-skill:export.format"], "mp4")
        self.assertEqual(export_properties["video-editing-skill:export.video_codec"], "libx264")
        profile_properties = {
            item.get("name"): item.text
            for item in compiled.findall("./tractor[@id='ves_main']/property")
            if item.get("name", "").startswith("video-editing-skill:profile.")
        }
        self.assertEqual(profile_properties["video-editing-skill:profile.width"], "1920")
        self.assertEqual(profile_properties["video-editing-skill:profile.height"], "1080")
        self.assertEqual(profile_properties["video-editing-skill:profile.frame_rate_num"], "30000")
        self.assertEqual(profile_properties["video-editing-skill:profile.frame_rate_den"], "1001")

    def test_caption_css_rgba_is_converted_to_mlt_argb(self) -> None:
        plan = valid_plan(self.asset)
        plan["operations"] = [{
            "id": "caption", "type": "caption", "target": "c1", "start": 0,
            "duration": 10, "text": "Gold", "color": "#fdb927ff", "background": "#00000099",
        }]
        root = compile_mlt(validate_plan(plan, source=self.root / "plan.json"), self.root / "out.mlt").getroot()
        caption_filter = root.find("./producer/filter[@id='ves_caption_filter_caption']")
        self.assertIsNotNone(caption_filter)
        properties = {item.get("name"): item.text for item in caption_filter.findall("property")}
        self.assertEqual(properties["fgcolour"], "#fffdb927")
        self.assertEqual(properties["bgcolour"], "#99000000")

    def test_transform_keyframes_replace_static_affine_properties(self) -> None:
        plan = valid_plan(self.asset)
        plan["operations"] = [{
            "id": "move", "type": "transform", "target": "c1",
            "geometry": "0%/0%:100%x100%", "opacity": 0.25,
            "keyframes": [
                {"frame": 0, "geometry": "-10%/0%:120%x120%", "opacity": 0.5},
                {"frame": 10, "geometry": "0%/0%:100%x100%", "opacity": 1},
            ],
        }]
        root = compile_mlt(validate_plan(plan, source=self.root / "plan.json"), self.root / "out.mlt").getroot()
        transform = root.find("./producer/filter[@id='ves_filter_move']")
        self.assertIsNotNone(transform)
        geometries = transform.findall("./property[@name='transition.rect']")
        mixes = transform.findall("./property[@name='transition.mix']")
        self.assertEqual([item.text for item in geometries], ["0=-10%/0%:120%x120%;10=0%/0%:100%x100%"])
        self.assertEqual([item.text for item in mixes], ["0=0.5;10=1"])
        self.assertEqual(transform.find("./property[@name='shotcut:filter']").text, "affineSizePosition")
        self.assertEqual(transform.find("./property[@name='transition.fix_rotate_x']").text, "0")
        self.assertIsNone(transform.find("./property[@name='transition.rotate_x']"))

    def test_compiler_rejects_unsupported_transform_properties_if_validation_is_bypassed(self) -> None:
        plan = valid_plan(self.asset)
        plan["operations"] = [{
            "id": "move", "type": "transform", "target": "c1",
            "keyframes": [{"frame": 0, "geometry": "0%/0%:100%x100%", "vendor": "ignored"}],
        }]
        validated = ValidatedPlan(plan, self.root / "plan.json")
        with self.assertRaisesRegex(VideoEditingError, "unsupported transform keyframe"):
            compile_mlt(validated, self.root / "out.mlt")

    def test_write_never_changes_media_or_overwrites_output(self) -> None:
        output = self.root / "project.mlt"
        write_mlt(self.validated(), output)
        self.assertEqual(self.asset.read_bytes(), self.before)
        with self.assertRaises(VideoEditingError):
            write_mlt(self.validated(), output)

    def test_base_unknown_xml_survives_semantically(self) -> None:
        base = self.root / "base.mlt"
        base.write_text("""<?xml version='1.0'?><mlt><profile width='1920' height='1080' frame_rate_num='30000' frame_rate_den='1001'/><producer id='foreign'><property name='vendor:x'>keep &amp; preserve</property><mystery value='42'/></producer></mlt>""", encoding="utf-8")
        output = self.root / "derived.mlt"
        write_mlt(self.validated(), output, base_project=base)
        root = ET.parse(output).getroot()
        self.assertEqual(root.find("./producer[@id='foreign']/property").text, "keep & preserve")
        self.assertEqual(root.find("./producer[@id='foreign']/mystery").get("value"), "42")
        self.assertIn("foreign", base.read_text(encoding="utf-8"))

    def test_hidden_track_has_an_explicit_mlt_effect(self) -> None:
        plan = valid_plan(self.asset)
        plan["tracks"][0]["hidden"] = True
        root = compile_mlt(validate_plan(plan, source=self.root / "plan.json"), self.root / "out.mlt").getroot()
        tracks = root.findall("./tractor[@id='ves_main']/multitrack/track")
        self.assertEqual(tracks[1].get("hide"), "video")

    def test_track_kinds_route_streams_and_audio_is_not_composited_over_video(self) -> None:
        plan = valid_plan(self.asset)
        plan["tracks"].append({
            "id": "a1", "kind": "audio", "name": "Audio",
            "clips": [{
                "id": "a1_clip", "asset_id": "a", "timeline_start": 0,
                "source_in": 0, "duration": 100,
            }],
        })
        root = compile_mlt(validate_plan(plan, source=self.root / "plan.json"), self.root / "out.mlt").getroot()
        tracks = root.findall("./tractor[@id='ves_main']/multitrack/track")
        self.assertEqual([track.get("hide") for track in tracks], [None, None, "video"])
        self.assertIsNotNone(root.find("./tractor[@id='ves_main']/transition[@id='ves_composite_1']"))
        self.assertIsNone(root.find("./tractor[@id='ves_main']/transition[@id='ves_composite_2']"))
        self.assertIsNotNone(root.find("./tractor[@id='ves_main']/transition[@id='ves_audio_mix_track_2']"))

    def test_brightness_has_shotcut_identity_and_mlt_level(self) -> None:
        root = compile_mlt(self.validated(), self.root / "out.mlt").getroot()
        brightness = root.find("./producer/filter[@id='ves_filter_filter']")
        self.assertIsNotNone(brightness)
        properties = {item.get("name"): item.text for item in brightness.findall("property")}
        self.assertEqual(properties["mlt_service"], "brightness")
        self.assertEqual(properties["shotcut:filter"], "brightness")
        self.assertEqual(properties["level"], "1.1")

    def test_fractional_brightness_amount_is_not_darkening(self) -> None:
        plan = valid_plan(self.asset)
        plan["operations"] = [{
            "id": "brighten",
            "type": "filter",
            "target": "c1",
            "name": "brightness",
            "properties": {"level": 0.35},
        }]
        validated = validate_plan(plan, source=self.root / "plan.json")
        root = compile_mlt(validated, self.root / "fractional.mlt").getroot()
        node = root.find("./producer/filter[@id='ves_filter_brighten']")
        self.assertIsNotNone(node)
        properties = {item.get("name"): item.text for item in node.findall("property")}
        self.assertEqual(properties["level"], "1.35")


if __name__ == "__main__":
    unittest.main()
