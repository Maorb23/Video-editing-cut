from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from video_editing.analysis import AnalysisArtifact
from video_editing.benchmark import load_benchmark, run_instruction_benchmark
from video_editing.planning import ModelResponse
from video_editing.plan import OP_TYPES
from video_editing.probe import fingerprint


class RuleModel:
    def generate(self, **kwargs: Any) -> ModelResponse:
        request = json.loads(kwargs["input_text"])
        instruction = request["instruction"]
        operation: dict[str, Any] | None = None
        duration, source_in = 60, 0
        if "first second" in instruction and "Remove" in instruction:
            operation = {"id": "trim", "type": "trim", "target": "c1", "source_in": 30, "duration": 30, "enabled": True}
        elif "caption" in instruction:
            operation = {"id": "caption", "type": "caption", "target": "c1", "start": 0, "duration": 30, "text": "Hello", "enabled": True}
        elif "zoom" in instruction:
            operation = {"id": "zoom", "type": "transform", "target": "c1", "geometry": "-10%/-10%:120%x120%", "enabled": True}
        elif "Fade" in instruction:
            operation = {"id": "fade", "type": "fade_audio", "target": "c1", "start": 30, "duration": 30, "direction": "out", "enabled": True}
        return ModelResponse({
            "summary": instruction, "unsupported": [],
            "tracks": [{
                "id": "v1", "kind": "video", "name": "Video", "muted": False, "hidden": False,
                "clips": [{"id": "c1", "asset_id": "source", "timeline_start": 0, "source_in": source_in, "duration": duration, "enabled": True}],
            }],
            "operations": [] if operation is None else [operation],
            "export": {"format": "mp4", "video_codec": "libx264", "audio_codec": "aac", "pixel_format": "yuv420p", "movflags": "+faststart"},
        }, {"provider": "rule-fixture"})


class InstructionBenchmarkTests(unittest.TestCase):
    def test_v1_benchmark_is_nonempty_unique_and_within_capabilities(self) -> None:
        path = Path(__file__).parents[1] / "benchmarks" / "v1" / "manifest.jsonl"
        cases = load_benchmark(path)
        self.assertGreaterEqual(len(cases), 5)
        self.assertEqual(len({case["id"] for case in cases}), len(cases))
        for case in cases:
            self.assertTrue(case["instruction"].strip())
            self.assertTrue(set(case["expected_operations"]).issubset(OP_TYPES))

    def test_benchmark_executes_natural_language_through_planner(self) -> None:
        manifest = Path(__file__).parents[1] / "benchmarks" / "v1" / "manifest.jsonl"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.mp4"
            source.write_bytes(b"video")
            data = {
                "source": {
                    "duration_frames": 60, "fingerprint": fingerprint(source),
                    "video": {"width": 160, "height": 90},
                    "audio": {"codec": "aac", "sample_rate": "48000", "channels": 2},
                },
                "timeline_policy": {"frame_rate": {"numerator": 30, "denominator": 1}, "duration_frames": 60},
                "observations": [],
            }
            analysis_path = root / "analysis.json"
            analysis_path.write_text(json.dumps(data), encoding="utf-8")
            result = run_instruction_benchmark(
                RuleModel(), manifest, AnalysisArtifact(data, analysis_path, ()),
                plan_directory=root, source_relative="source.mp4",
            )
        self.assertEqual(result.passed, result.total)
        self.assertEqual(result.total, 5)


if __name__ == "__main__":
    unittest.main()
