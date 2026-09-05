from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from video_editing.errors import PlanValidationError
from video_editing.plan import validate_plan

from tests.helpers import valid_plan


class ValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.asset = self.root / "space's ünicode.mp4"
        self.asset.write_bytes(b"media")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def validate(self, plan: dict) -> None:
        validate_plan(plan, source=self.root / "edit-plan.json")

    def codes(self, plan: dict) -> set[str]:
        with self.assertRaises(PlanValidationError) as context:
            self.validate(plan)
        return {issue.code for issue in context.exception.issues}

    def test_valid_plan_with_unicode_and_apostrophe(self) -> None:
        self.validate(valid_plan(self.asset))

    def test_unknown_operation_and_property_are_rejected(self) -> None:
        plan = valid_plan(self.asset)
        plan["operations"] = [{"id": "x", "type": "magic", "target": "c1"}]
        plan["surprise"] = True
        self.assertTrue({"unknown_operation", "unknown_property"}.issubset(self.codes(plan)))

    def test_missing_asset_and_source_overrun(self) -> None:
        plan = valid_plan(self.asset)
        plan["assets"][0]["path"] = "absent.mp4"
        plan["tracks"][0]["clips"][0]["source_in"] = 250
        plan["tracks"][0]["clips"][0]["duration"] = 100
        self.assertTrue({"missing_asset", "invalid_range"}.issubset(self.codes(plan)))

    def test_overlapping_clips_rejected(self) -> None:
        plan = valid_plan(self.asset)
        plan["tracks"][0]["clips"].append({"id": "c2", "asset_id": "a", "timeline_start": 50, "source_in": 0, "duration": 20})
        self.assertIn("incompatible_overlap", self.codes(plan))

    def test_curated_filter_properties_are_strict(self) -> None:
        plan = valid_plan(self.asset)
        plan["operations"] = [{"id": "f", "type": "filter", "target": "c1", "name": "brightness", "properties": {"arbitrary": 1}}]
        self.assertIn("unsupported_filter_property", self.codes(plan))

    def test_profile_must_be_rational(self) -> None:
        plan = valid_plan(self.asset)
        plan["profile"]["frame_rate"] = {"numerator": 29.97, "denominator": 1}
        self.assertIn("invalid_profile", self.codes(plan))

    def test_insert_that_creates_overlap_is_rejected(self) -> None:
        plan = valid_plan(self.asset)
        plan["operations"] = [{
            "id": "insert", "type": "insert", "track_id": "v1",
            "clip": {"id": "c2", "asset_id": "a", "timeline_start": 50, "source_in": 0, "duration": 20},
        }]
        self.assertIn("incompatible_overlap", self.codes(plan))

    def test_transform_rejects_unsupported_interpolation(self) -> None:
        plan = valid_plan(self.asset)
        plan["operations"] = [{
            "id": "move", "type": "transform", "target": "c1", "interpolation": "spline",
        }]
        self.assertIn("unsupported_transform_property", self.codes(plan))

    def test_fingerprint_is_verified(self) -> None:
        plan = valid_plan(self.asset)
        plan["assets"][0]["fingerprint"] = "sha256:" + "0" * 64
        self.assertIn("fingerprint_mismatch", self.codes(plan))

    def test_structural_and_effect_ranges_are_revalidated(self) -> None:
        plan = valid_plan(self.asset)
        plan["operations"] = [{"id": "trim", "type": "trim", "target": "c1", "source_in": 250, "duration": 100}]
        self.assertIn("invalid_range", self.codes(plan))
        plan = valid_plan(self.asset)
        plan["operations"] = [{"id": "move", "type": "transform", "target": "c1", "start": 99, "duration": 2}]
        self.assertIn("invalid_range", self.codes(plan))

    def test_legacy_v1_operation_properties_remain_accepted(self) -> None:
        plan = valid_plan(self.asset)
        plan["operations"] = [{"id": "mix", "type": "audio_mix", "target": "v1", "pan": 0.5}]
        self.validate(plan)
        plan = valid_plan(self.asset)
        plan["operations"] = [{"id": "speed", "type": "speed", "target": "c1", "factor": 2, "start": 0}]
        self.validate(plan)

    def test_transition_cannot_reference_a_removed_clip(self) -> None:
        plan = valid_plan(self.asset)
        plan["tracks"].append({
            "id": "v2", "kind": "video", "clips": [
                {"id": "c2", "asset_id": "a", "timeline_start": 0, "source_in": 0, "duration": 100},
            ],
        })
        plan["operations"] = [
            {"id": "remove", "type": "remove", "target": "c2"},
            {"id": "transition", "type": "transition", "from_clip_id": "c1", "to_clip_id": "c2", "duration": 10},
        ]
        self.assertIn("missing_target", self.codes(plan))

    def test_standalone_path_confinement_is_opt_in_for_cli_compatibility(self) -> None:
        plan = valid_plan(self.asset)
        plan["assets"][0]["path"] = "../outside.mp4"
        with self.assertRaises(PlanValidationError) as context:
            validate_plan(plan, source=self.root / "edit-plan.json", check_files=False, require_confined_paths=True)
        self.assertIn("unsafe_path", {issue.code for issue in context.exception.issues})


if __name__ == "__main__":
    unittest.main()
