#!/usr/bin/env python3
"""Suite for the directory the deploy clears, and the bundle's line format.

`prepare` runs `rm -rf` on the path it resolves, and that is the whole reason
these cases exist. diagnostics_path.py decides which VALUES are refusable and
has its own suite; this one covers what happens around that decision on a real
filesystem - the second check after the value is made absolute, and the
ownership marker that is the only thing standing between a mistyped variable and
an operator's home directory.

`compact` is here for a smaller reason with the same shape: the bundle is parsed
by splitting on lines, so a newline inside a value invents a field. The value is
wrangler's own text, which can carry anything.
"""

from __future__ import annotations

import contextlib
import pathlib
import sys
import tempfile
import unittest
from typing import override

MODULE = (
    pathlib.Path(__file__).resolve().parents[2] / "deploy-lib" / "deploy_diagnostics.py"
)

sys.path.insert(0, str(MODULE.parent))

import deploy_diagnostics
import deploy_reporting


def silent_reporter() -> deploy_reporting.Reporter:
    return deploy_reporting.Reporter(github_actions=False, step_summary_path=None)


class DirectoryOwnership(unittest.TestCase):
    @override
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.root = pathlib.Path(self._temp.name)

    def prepare(self, configured: str) -> deploy_diagnostics.Diagnostics:
        diagnostics = deploy_diagnostics.Diagnostics(
            configured, reporter=silent_reporter()
        )
        diagnostics.prepare()
        return diagnostics

    def test_an_unset_directory_leaves_the_feature_off(self) -> None:
        diagnostics = deploy_diagnostics.Diagnostics(None, reporter=silent_reporter())
        diagnostics.prepare()
        self.assertFalse(diagnostics.enabled)
        self.assertTrue(diagnostics.write_bundle([("exit_code", "0")]))

    def test_a_fresh_directory_is_claimed_with_an_ownership_marker(self) -> None:
        target = self.root / "artifacts"
        self.prepare(str(target))
        self.assertTrue((target / deploy_diagnostics.OWNER_MARKER).is_file())
        self.assertTrue((target / deploy_diagnostics.START_MARKER).is_file())

    def test_re_preparing_a_directory_this_library_owns_clears_it(self) -> None:
        target = self.root / "artifacts"
        self.prepare(str(target))
        stale = target / "previous-run.txt"
        stale.write_text("from the last deploy", encoding="utf-8")
        self.prepare(str(target))
        self.assertFalse(stale.exists())
        self.assertTrue((target / deploy_diagnostics.OWNER_MARKER).is_file())

    def test_a_trailing_separator_is_the_same_directory(self) -> None:
        # POSIX `dirname` strips trailing separators before splitting and
        # Python's does not, so a CI job exporting `.../artifacts/` used to have
        # the preparer create the target and then refuse it as a directory it
        # did not own - the value the operator wrote and the value the guard
        # judged were different strings.
        # Built as a string rather than through `/`, which normalizes a
        # trailing separator away - the test would then drive the same value
        # every other case does and pass whatever the preparer did with it.
        for suffix in ("/", "//"):
            with self.subTest(suffix=suffix):
                self.prepare(f"{self.root}/artifacts{suffix}")
                self.assertTrue(
                    (
                        self.root / "artifacts" / deploy_diagnostics.OWNER_MARKER
                    ).is_file()
                )

    def test_an_existing_directory_without_the_marker_is_refused(self) -> None:
        # The case the marker exists for: a mistyped variable pointing at a real
        # directory. Refusing costs a re-run; clearing it costs the directory.
        target = self.root / "someone-elses"
        target.mkdir()
        (target / "important.txt").write_text("keep me", encoding="utf-8")
        with self.assertRaises(deploy_diagnostics.Refused):
            self.prepare(str(target))
        self.assertTrue((target / "important.txt").is_file())

    def test_a_traversal_component_is_refused_before_anything_is_created(self) -> None:
        target = self.root / "artifacts" / ".." / "elsewhere"
        with self.assertRaises(deploy_diagnostics.Refused):
            self.prepare(str(target))
        self.assertFalse((self.root / "elsewhere").exists())

    def test_a_separator_only_value_is_refused(self) -> None:
        # `//` names no directory, and the preparation step's dirname and
        # basename are `/` - so the path it would clear is the filesystem
        # root.
        for value in ("/", "//", "///", ""):
            with (
                self.subTest(value=value),
                self.assertRaises(deploy_diagnostics.Refused),
            ):
                self.prepare(value)

    def test_a_relative_value_resolves_against_the_working_directory(self) -> None:
        # The second safety check runs on the ABSOLUTE string, not the one the
        # operator wrote, because the absolute one is what the delete targets -
        # and a missing parent is created rather than refused, which is what
        # lets a CI job name an artifact path that does not exist yet.
        with contextlib.chdir(self.root):
            diagnostics = self.prepare("nested/artifacts")
        self.assertTrue(diagnostics.enabled)
        self.assertTrue(
            (
                self.root / "nested" / "artifacts" / deploy_diagnostics.OWNER_MARKER
            ).is_file()
        )

    def test_the_bundle_lands_whole_under_the_name_operators_read(self) -> None:
        target = self.root / "artifacts"
        diagnostics = self.prepare(str(target))
        self.assertTrue(
            diagnostics.write_bundle([("exit_code", "1"), ("env", "staging")])
        )
        self.assertEqual(
            (target / deploy_diagnostics.BUNDLE_NAME).read_text(encoding="utf-8"),
            "exit_code=1\nenv=staging\n",
        )


class RecursiveDelete(unittest.TestCase):
    """The guard on the delete itself, which is the operation with consequences."""

    def test_the_delete_refuses_a_path_the_gate_would_not_accept(self) -> None:
        # No value `prepare` can produce reaches this today - it refuses the
        # operator's string first, and making a path absolute normalizes a
        # traversal away. That is the property the pair maintains rather than a
        # gap in it: the check sits on the recursive delete, so a future
        # resolution step cannot hand it a target nobody checked.
        for value in ("/", "//", "/tmp/artifacts/../elsewhere"):
            with (
                self.subTest(value=value),
                self.assertRaises(deploy_diagnostics.Refused),
            ):
                deploy_diagnostics.remove_target(pathlib.Path(value))


class ValueCompaction(unittest.TestCase):
    def test_a_newline_in_a_value_cannot_invent_a_field(self) -> None:
        self.assertEqual(
            deploy_diagnostics.compact("first line\nexit_code=0"),
            "first line exit_code=0",
        )

    def test_runs_of_ascii_whitespace_collapse_and_the_edges_go(self) -> None:
        self.assertEqual(deploy_diagnostics.compact("  a \t\r\n b  "), "a b")

    def test_a_value_with_nothing_in_it_stays_empty(self) -> None:
        self.assertEqual(deploy_diagnostics.compact(""), "")
        self.assertEqual(deploy_diagnostics.compact("\n\n"), "")


if __name__ == "__main__":
    unittest.main()
