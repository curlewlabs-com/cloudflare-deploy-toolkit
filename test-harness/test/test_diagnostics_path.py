#!/usr/bin/env python3
"""Suite for the guard on the deploy's diagnostics directory.

This decides an `rm -rf` target, so the cases are grouped by what a wrong answer
costs. Accepting a traversal means a recursive delete somewhere the operator did
not name; refusing a legitimate path means a deploy that will not start over a
directory name. The first is why the guard exists, and most of the cases below
are shapes that must be refused.
"""

from __future__ import annotations

import pathlib
import sys
import unittest

MODULE = (
    pathlib.Path(__file__).resolve().parents[2] / "deploy-lib" / "diagnostics_path.py"
)

sys.path.insert(0, str(MODULE.parent))

import diagnostics_path


class PathCase(unittest.TestCase):
    def assertAccepted(self, candidate: str, why: str) -> None:
        self.assertTrue(diagnostics_path.is_safe(candidate), why)

    def assertRefused(self, candidate: str, why: str) -> None:
        self.assertFalse(diagnostics_path.is_safe(candidate), why)


class Refusals(PathCase):
    """The shapes that would put `rm -rf` somewhere nobody named."""

    def test_a_dot_or_dotdot_component_anywhere_is_refused(self) -> None:
        for candidate in (
            ".",
            "..",
            "./artifacts",
            "../artifacts",
            "/tmp/./artifacts",
            "/tmp/../artifacts",
            "/tmp/artifacts/.",
            "/tmp/artifacts/..",
            "a/./b",
            "a/../b",
            "/.",
            "/..",
            "/tmp/artifacts/./",
        ):
            with self.subTest(candidate=candidate):
                self.assertRefused(
                    candidate,
                    "the parent canonicalizes cleanly, which is exactly what hides this",
                )

    def test_the_empty_string_is_refused(self) -> None:
        self.assertRefused("", "an unset-looking value names no directory to own")

    def test_every_separator_only_form_of_the_root_is_refused(self) -> None:
        # `//` is the one that reads as harmless. It has a real component in
        # neither position, and the preparation step rejoins its dirname and
        # basename - each `/` - so the directory cleanup deletes is the
        # filesystem root. `/` alone is the form anyone thinks to check.
        for candidate in ("/", "//", "///", "/////"):
            with self.subTest(candidate=candidate):
                self.assertRefused(
                    candidate, "a value of only separators names no directory to own"
                )


class Acceptances(PathCase):
    """Legitimate directory names, including ones that merely look like dots."""

    def test_ordinary_absolute_and_relative_paths_are_accepted(self) -> None:
        # `//tmp//artifacts` and the trailing-slash forms are here on purpose:
        # the separator-only refusal above must not reach a path that has
        # doubled separators around a real component.
        for candidate in (
            "/tmp/artifacts",
            "artifacts",
            "/tmp/artifacts/",
            "//tmp//artifacts",
            "/tmp/artifacts//",
            "//artifacts",
            "/a/b/c",
            "/tmp/ar tifacts",
        ):
            with self.subTest(candidate=candidate):
                self.assertAccepted(candidate, "a plain directory name is usable")

    def test_dots_inside_a_component_are_not_traversal(self) -> None:
        # The check is about whole components. Refusing these would refuse a
        # legitimate directory over the characters in its name.
        for candidate in (
            ".hidden",
            "/tmp/.hidden",
            "/tmp/..config",
            "/tmp/a.b/c",
            "/tmp/a..b",
        ):
            with self.subTest(candidate=candidate):
                self.assertAccepted(
                    candidate, "a dot in a name is not a path component"
                )


if __name__ == "__main__":
    unittest.main()
