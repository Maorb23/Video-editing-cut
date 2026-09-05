from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from video_editing.errors import VideoEditingError
from video_editing.planning import OpenAIResponsesModel, edit_plan_draft_schema


class Response:
    def __init__(self, value: dict) -> None:
        self.value = json.dumps(value).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def read(self, limit: int) -> bytes:
        return self.value


class OpenAIProviderTests(unittest.TestCase):
    @patch("video_editing.planning.openai.urllib.request.urlopen")
    def test_responses_request_is_schema_constrained_and_toolless(self, urlopen) -> None:
        urlopen.return_value = Response({
            "id": "resp_test", "status": "completed", "model": "test-model",
            "output_text": '{"ok":true}', "usage": {"total_tokens": 3},
        })
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "frame.jpg"
            image.write_bytes(b"jpeg")
            model = OpenAIResponsesModel(model="test-model", api_key="secret")
            schema = edit_plan_draft_schema()
            result = model.generate(
                instructions="policy", input_text="evidence", schema_name="video_edit_plan_draft_v1",
                schema=schema,
                images=(image,),
            )
        request = urlopen.call_args.args[0]
        payload = json.loads(request.data)
        self.assertEqual(payload["text"]["format"], {
            "type": "json_schema",
            "name": "video_edit_plan_draft_v1",
            "strict": True,
            "schema": schema,
        })
        self.assertEqual(payload["tools"], [])
        self.assertFalse(payload["store"])
        self.assertEqual(payload["input"][0]["content"][1]["type"], "input_image")
        self.assertEqual(result.data, {"ok": True})

    def test_oversized_analysis_image_is_rejected_before_request(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "frame.jpg"
            image.write_bytes(b"x" * (OpenAIResponsesModel.MAX_IMAGE_BYTES + 1))
            model = OpenAIResponsesModel(model="test-model", api_key="secret")
            with self.assertRaises(VideoEditingError) as context:
                model.generate(
                    instructions="policy", input_text="evidence", schema_name="test",
                    schema={"type": "object", "properties": {}, "required": [], "additionalProperties": False},
                    images=(image,),
                )
        self.assertEqual(context.exception.code, "model_input_too_large")


if __name__ == "__main__":
    unittest.main()
