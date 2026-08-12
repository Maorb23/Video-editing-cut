from __future__ import annotations

import unittest
from fractions import Fraction

from video_editing.timebase import frames_to_mlt_time, parse_seconds, seconds_to_frames


class TimebaseTests(unittest.TestCase):
    def test_rational_ntsc_conversion_is_exact(self) -> None:
        self.assertEqual(seconds_to_frames("1001/1000", 30000, 1001), 30)
        self.assertEqual(parse_seconds(0.1), Fraction(1, 10))

    def test_rounding_modes(self) -> None:
        self.assertEqual(seconds_to_frames("1/10", 24, 1, rounding="floor"), 2)
        self.assertEqual(seconds_to_frames("1/10", 24, 1, rounding="ceil"), 3)
        self.assertEqual(seconds_to_frames("1/10", 24, 1), 2)

    def test_mlt_clock(self) -> None:
        self.assertEqual(frames_to_mlt_time(30, 30000, 1001), "00:00:01.001000")


if __name__ == "__main__":
    unittest.main()

