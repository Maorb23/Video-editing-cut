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


if __name__ == "__main__":
    unittest.main()
