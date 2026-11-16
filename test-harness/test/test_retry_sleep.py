#!/usr/bin/env python3
"""Suite for the wait between deploy attempts.

The decision and the randomness are separate functions, which is what lets
every row here be deterministic: `sleep_seconds` is driven with each jitter
value explicitly, and `draw_jitter` is checked for its range against a seeded
source rather than for any particular draw.
"""

from __future__ import annotations

import pathlib
import random
import sys
import unittest

MODULE = pathlib.Path(__file__).resolve().parents[2] / "deploy-lib" / "retry_sleep.py"

sys.path.insert(0, str(MODULE.parent))

import retry_sleep


class TheWait(unittest.TestCase):
    def test_a_zero_base_waits_not_at_all(self) -> None:
        # The harnesses set the schedule to zeros; a jittered zero would put a
        # random second back into a suite that asked for none.
        for jitter in (0, 1):
            with self.subTest(jitter=jitter):
                self.assertEqual(retry_sleep.sleep_seconds(0, jitter), 0)

    def test_a_negative_base_waits_not_at_all(self) -> None:
        for base in (-1, -5, -100):
            with self.subTest(base=base):
                self.assertEqual(retry_sleep.sleep_seconds(base, 1), 0)

    def test_a_one_second_base_takes_no_jitter(self) -> None:
        # Spreading a one-second wait across its full jitter range doubles it; the schedule the
        # caller wrote stops being the schedule it gets.
        for jitter in (0, 1):
            with self.subTest(jitter=jitter):
                self.assertEqual(retry_sleep.sleep_seconds(1, jitter), 1)

    def test_a_longer_base_takes_the_jitter_it_is_given(self) -> None:
        for base in (2, 3, 8, 15, 100):
            for jitter in (0, 1):
                with self.subTest(base=base, jitter=jitter):
                    self.assertEqual(
                        retry_sleep.sleep_seconds(base, jitter), base + jitter
                    )


class TheDraw(unittest.TestCase):
    def test_the_draw_stays_inside_the_declared_bound(self) -> None:
        # Range, not distribution: nothing depends on how the second is spread,
        # only that the schedule is never stretched further than declared.
        rng = random.Random(20260905)
        drawn = {retry_sleep.draw_jitter(rng) for _ in range(10_000)}
        self.assertEqual(drawn, {0, retry_sleep.MAX_JITTER_SECONDS})
        self.assertLessEqual(max(drawn), retry_sleep.MAX_JITTER_SECONDS)


if __name__ == "__main__":
    unittest.main()
