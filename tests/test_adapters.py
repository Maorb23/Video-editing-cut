from __future__ import annotations

import unittest

from video_editing.adapters import validate_transcript, validate_visual_analysis
from video_editing.errors import PlanValidationError


class AdapterTests(unittest.TestCase):
    def test_transcript_interface(self) -> None:
        value = {"version": "1.0", "segments": [{"start_frame": 0, "end_frame": 12, "text": "Hello"}]}
        self.assertIs(validate_transcript(value), value)

    def test_visual_interface_rejects_bad_frames(self) -> None:
        with self.assertRaises(PlanValidationError):
            validate_visual_analysis({"version": "1.0", "observations": [{"frame": "one", "description": "cut"}]})


if __name__ == "__main__":
    unittest.main()

