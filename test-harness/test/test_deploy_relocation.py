#!/usr/bin/env python3
"""Suite for the states a relocation may start from.

The deploy locks of the source and the target are a relocation's only durable
record, so this decision is the whole of what a re-dispatch knows about the run
before it. Each refusal below is a state where going ahead would lose writes or
hide them: an open target is already serving, and a locked copy under an open
source is stale the moment the source takes its next write.
"""

from __future__ import annotations

import pathlib
import sys
import unittest

MODULE = (
    pathlib.Path(__file__).resolve().parents[2] / "deploy-lib" / "deploy_relocation.py"
)

sys.path.insert(0, str(MODULE.parent))

import deploy_relocation

LOCKED, UNLOCKED = "locked", "unlocked"


def start(
    source: str, target: str, *, absent: bool, resume: bool = False
) -> deploy_relocation.Start:
    return deploy_relocation.start_from(
        source, target, target_absent=absent, resume_acknowledged=resume
    )


class StartStates(unittest.TestCase):
    def test_an_open_source_and_a_target_never_filled_is_a_fresh_start(self) -> None:
        self.assertFalse(start(UNLOCKED, UNLOCKED, absent=True).resumed)

    def test_a_fenced_source_resumes_whether_or_not_a_copy_landed(self) -> None:
        for target, absent in ((UNLOCKED, True), (LOCKED, False)):
            with self.subTest(target=target, absent=absent):
                self.assertTrue(
                    start(LOCKED, target, absent=absent, resume=True).resumed
                )

    def test_a_fenced_source_nobody_vouched_for_refuses(self) -> None:
        # The lock is one bit. A deploy that died inside its window leaves the
        # same one an earlier relocation run does.
        for target, absent in ((UNLOCKED, True), (LOCKED, False)):
            with (
                self.subTest(target=target, absent=absent),
                self.assertRaisesRegex(deploy_relocation.Refused, "who set it"),
            ):
                start(LOCKED, target, absent=absent)

    def test_a_resume_with_nothing_fenced_refuses(self) -> None:
        with self.assertRaisesRegex(deploy_relocation.Refused, "not fenced"):
            start(UNLOCKED, UNLOCKED, absent=True, resume=True)

    def test_an_open_target_refuses_whatever_the_source_reads(self) -> None:
        # A lock that READS clear is not a lock table that is absent: the first
        # is a database already taking writes, and copying over it would
        # destroy them.
        for source in (LOCKED, UNLOCKED):
            with (
                self.subTest(source=source),
                self.assertRaisesRegex(deploy_relocation.Refused, "already open"),
            ):
                start(source, UNLOCKED, absent=False, resume=source == LOCKED)

    def test_a_locked_copy_under_an_open_source_refuses(self) -> None:
        with self.assertRaisesRegex(deploy_relocation.Refused, "stale"):
            start(UNLOCKED, LOCKED, absent=False)

    def test_a_lock_nobody_could_read_refuses(self) -> None:
        # deploy_lock's value for an unreadable lock. Unreadable is not clear.
        for source, target in (("unknown", UNLOCKED), (UNLOCKED, "unknown")):
            with (
                self.subTest(source=source, target=target),
                self.assertRaisesRegex(deploy_relocation.Refused, "could not be read"),
            ):
                start(source, target, absent=True)


if __name__ == "__main__":
    unittest.main()
