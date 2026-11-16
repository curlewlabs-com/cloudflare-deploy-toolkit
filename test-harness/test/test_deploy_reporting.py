#!/usr/bin/env python3
"""Suite for the one rule the summary writer has: it never raises.

The step summary is operator-facing logging, and the recovery path writes to it
between the failure report and the D1 restore, worker rollback and unlock. An
exception out of `summary` stops the deploy there - maintenance version serving,
database still locked, no recovery attempted - and the next deploy then refuses
to start on a lock it cannot prove is clear. So the cases below are about what
the writer swallows, not about what it produces.

The pipeline-level proof that recovery survives a broken summary is the bats
case that drives a whole deploy. These cases cover the failure members that case
cannot reach: an unwritable file is easy to arrange from outside, an unencodable
line is not.
"""

from __future__ import annotations

import contextlib
import io
import pathlib
import sys
import tempfile
import unittest
from typing import override

MODULE = (
    pathlib.Path(__file__).resolve().parents[2] / "deploy-lib" / "deploy_reporting.py"
)

sys.path.insert(0, str(MODULE.parent))

import deploy_reporting

# What wrangler_runner.py's surrogateescape decode puts in a str when the
# Cloudflare API renders a byte that is not UTF-8. It reaches the summary
# through the version ids, the bookmark and the lock state the failure table
# reports, and encoding it raises UnicodeEncodeError - a ValueError, so an
# `except OSError` around the write does not see it.
LONE_SURROGATE = b"\xff".decode("utf-8", errors="surrogateescape")


class SummaryIsBestEffort(unittest.TestCase):
    @override
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.root = pathlib.Path(self._temp.name)

    def reporter_and_stderr(
        self, path: pathlib.Path
    ) -> tuple[deploy_reporting.Reporter, io.StringIO]:
        """A reporter writing to `path`, with its stderr captured."""
        captured = io.StringIO()
        context = contextlib.redirect_stderr(captured)
        context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)
        return (
            deploy_reporting.Reporter(
                github_actions=False, step_summary_path=str(path)
            ),
            captured,
        )

    def test_unwritable_file_does_not_raise_and_reports_once(self) -> None:
        # A directory where the file should be: refused with EISDIR for any uid,
        # where a mode of 000 is no barrier to a root CI runner.
        target = self.root / "summary.md"
        target.mkdir()
        reporter, stderr = self.reporter_and_stderr(target)

        for line in ("### Deploy FAILED", "| Exit code | 1 |", "**Total: 12s**"):
            reporter.summary(line)

        self.assertEqual(stderr.getvalue().count("step summary write failed"), 1)

    def test_unencodable_line_does_not_raise(self) -> None:
        target = self.root / "summary.md"
        reporter, stderr = self.reporter_and_stderr(target)

        reporter.summary(f"| Previous version | {LONE_SURROGATE} |")

        self.assertEqual(stderr.getvalue().count("step summary write failed"), 1)

    def test_a_broken_summary_stops_being_written_to(self) -> None:
        # Not cosmetic: the recovery path writes a dozen more rows after the
        # file is gone, and a message per row buries the recovery output the
        # operator is reading. The first failure is the whole report.
        target = self.root / "summary.md"
        reporter, stderr = self.reporter_and_stderr(target)

        reporter.summary(f"| Bookmark | {LONE_SURROGATE} |")
        reporter.summary("| Maintenance live | true |")
        target.write_text("", encoding="utf-8")
        reporter.summary("| Lock state | locked |")

        self.assertEqual(stderr.getvalue().count("step summary write failed"), 1)
        # Writable again, and still not written to: the run gave up on the file,
        # not on the individual line that failed.
        self.assertEqual(target.read_text(encoding="utf-8"), "")

    def test_no_summary_configured_is_not_a_failure(self) -> None:
        # A local run has no GITHUB_STEP_SUMMARY, and must produce no report.
        captured = io.StringIO()
        reporter = deploy_reporting.Reporter(
            github_actions=False, step_summary_path=None
        )
        with contextlib.redirect_stderr(captured):
            reporter.summary("| Exit code | 1 |")
        self.assertEqual(captured.getvalue(), "")

    def test_a_writable_summary_still_gets_every_line(self) -> None:
        target = self.root / "summary.md"
        reporter, stderr = self.reporter_and_stderr(target)

        reporter.summary("### Deploy to staging")
        reporter.summary("| Step | Duration |")

        self.assertEqual(
            target.read_text(encoding="utf-8"),
            "### Deploy to staging\n| Step | Duration |\n",
        )
        self.assertEqual(stderr.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
