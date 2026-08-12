from __future__ import annotations

import unittest

from video_editing.operations import resolve_timeline


class OperationTests(unittest.TestCase):
    def test_trim_split_remove_insert_and_reorder(self) -> None:
        plan = {
            "tracks": [{"id": "v", "kind": "video", "clips": [
                {"id": "a", "asset_id": "asset", "timeline_start": 0, "source_in": 0, "duration": 20},
                {"id": "b", "asset_id": "asset", "timeline_start": 20, "source_in": 20, "duration": 10},
            ]}],
            "operations": [
                {"id": "trim", "type": "trim", "target": "a", "source_in": 2, "duration": 18},
                {"id": "split", "type": "split", "target": "a", "at": 10},
                {"id": "remove", "type": "remove", "target": "b"},
                {"id": "insert", "type": "insert", "track_id": "v", "clip": {"id": "c", "asset_id": "asset", "timeline_start": 18, "source_in": 0, "duration": 4}},
                {"id": "order", "type": "reorder", "track_id": "v", "clip_ids": ["c", "a", "a__split"]},
            ],
        }
        clips = resolve_timeline(plan)[0]["clips"]
        self.assertEqual([clip["id"] for clip in clips], ["c", "a", "a__split"])
        self.assertEqual([clip["timeline_start"] for clip in clips], [0, 4, 14])
        self.assertEqual(clips[1]["source_in"], 2)
        self.assertEqual(clips[2]["source_in"], 12)


if __name__ == "__main__":
    unittest.main()

