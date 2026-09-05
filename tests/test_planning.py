from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from video_editing.analysis import AnalysisArtifact
from video_editing.errors import PlanValidationError, VideoEditingError
from video_editing.planning import EditPlanner, ModelResponse, edit_plan_draft_schema
from video_editing.probe import fingerprint


def draft(*, duration: int = 30, operations: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "summary": "A concise requested edit.",
        "unsupported": [],
        "tracks": [{
            "id": "v1", "kind": "video", "name": "Video", "muted": False, "hidden": False,
            "clips": [{"id": "c1", "asset_id": "source", "timeline_start": 0, "source_in": 0, "duration": duration, "enabled": True}],
        }],
        "operations": operations or [],
        "export": {
            "format": "mp4", "video_codec": "libx264", "audio_codec": "aac",
            "video_bitrate": None, "audio_bitrate": "128k", "pixel_format": "yuv420p", "movflags": "+faststart",
        },
    }


class FakeModel:
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def generate(self, **kwargs: Any) -> ModelResponse:
        self.calls.append(kwargs)
        return ModelResponse(self.responses.pop(0), {"provider": "fake", "response_id": len(self.calls)})


class PlanningTests(unittest.TestCase):
    def analysis(self, root: Path, asset: Path, *, duration: int = 30) -> AnalysisArtifact:
        data = {
            "source": {
                "id": "source", "duration_frames": duration, "fingerprint": fingerprint(asset),
                "video": {"width": 160, "height": 90, "avg_frame_rate": "30/1", "r_frame_rate": "30/1"},
                "audio": None,
            },
            "timeline_policy": {"frame_rate": {"numerator": 30, "denominator": 1}, "duration_frames": duration},
            "observations": [],
        }
        analysis_path = root / "analysis.json"
        analysis_path.write_text(json.dumps(data), encoding="utf-8")
        return AnalysisArtifact(data, analysis_path, ())

    def test_schema_is_strict_and_covers_every_operation(self) -> None:
        schema = edit_plan_draft_schema()
        self.assertFalse(schema["additionalProperties"])
        variants = schema["properties"]["operations"]["items"]["anyOf"]
        self.assertEqual({item["properties"]["type"]["const"] for item in variants}, __import__("video_editing.plan", fromlist=["OP_TYPES"]).OP_TYPES)
        self.assertTrue(all(set(item["properties"]) == set(item["required"]) for item in variants))

    def test_schema_const_and_enum_nodes_have_explicit_compatible_types(self) -> None:
        def value_type(value: Any) -> str:
            if value is None:
                return "null"
            if isinstance(value, bool):
                return "boolean"
            if isinstance(value, str):
                return "string"
            if isinstance(value, int):
                return "integer"
            if isinstance(value, float):
                return "number"
            if isinstance(value, list):
                return "array"
            return "object"

        def audit(node: Any, path: str = "$") -> None:
            if isinstance(node, list):
                for index, item in enumerate(node):
                    audit(item, f"{path}[{index}]")
                return
            if not isinstance(node, dict):
                return
            if "const" in node or "enum" in node:
                self.assertIn("type", node, path)
                declared = node["type"]
                declared_types = {declared} if isinstance(declared, str) else set(declared)
                values = [node["const"]] if "const" in node else node["enum"]
                for value in values:
                    actual = value_type(value)
                    self.assertTrue(actual in declared_types or (actual == "integer" and "number" in declared_types), path)
            if node.get("type") == "object":
                self.assertEqual(set(node.get("required", [])), set(node.get("properties", {})), path)
                self.assertIs(node.get("additionalProperties"), False, path)
            for key, value in node.items():
                audit(value, f"{path}.{key}")

        audit(edit_plan_draft_schema())

    def test_invalid_model_plan_is_repaired_without_resending_images(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            asset = root / "source.mp4"
            asset.write_bytes(b"video")
            model = FakeModel([draft(duration=31), draft(duration=30)])
            planner = EditPlanner(model, max_repair_attempts=1)
            result = planner.plan("Keep the whole clip", self.analysis(root, asset), plan_path=root / "edit-plan.json", source_relative="source.mp4")
            self.assertEqual(len(result.attempts), 2)
            self.assertEqual(model.calls[1]["images"], ())
            self.assertIn("issues", json.loads(model.calls[1]["input_text"]))

    def test_repair_loop_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            asset = root / "source.mp4"
            asset.write_bytes(b"video")
            model = FakeModel([draft(duration=31), draft(duration=31)])
            with self.assertRaises(PlanValidationError):
                EditPlanner(model, max_repair_attempts=1).plan(
                    "Keep it", self.analysis(root, asset), plan_path=root / "edit-plan.json", source_relative="source.mp4"
                )
            self.assertEqual(len(model.calls), 2)

    def test_invalid_decision_log_is_repaired(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            asset = root / "source.mp4"
            asset.write_bytes(b"video")
            analysis = self.analysis(root, asset)
            analysis.data["observations"] = [{"path": "analysis/frames/sample-001.jpg"}]
            invalid = draft()
            invalid["decision_log"] = {
                "observations": [{"type": "visual", "description": "Subject", "evidence": ["sample-001.jpg"], "confidence": 0.9}],
                "decisions": [], "unsupported": [], "assumptions": [],
            }
            model = FakeModel([invalid, draft()])
            result = EditPlanner(model, max_repair_attempts=1).plan(
                "Keep it", analysis, plan_path=root / "edit-plan.json", source_relative="source.mp4",
            )
            self.assertEqual(len(result.attempts), 2)
            self.assertIn("decision log references unknown evidence", model.calls[1]["input_text"])

    def test_unsupported_instruction_stops_before_render(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            asset = root / "source.mp4"
            asset.write_bytes(b"video")
            value = draft()
            value["unsupported"] = ["face replacement"]
            with self.assertRaises(VideoEditingError) as context:
                EditPlanner(FakeModel([value])).plan(
                    "Replace a face", self.analysis(root, asset), plan_path=root / "edit-plan.json", source_relative="source.mp4"
                )
            self.assertEqual(context.exception.code, "unsupported_instruction")


if __name__ == "__main__":
    unittest.main()
