#!/usr/bin/env python3
"""Suite for the question asked of a value before the pipeline records it.

Inside recovery, the deploy spends the recorded serving version id as
`wrangler rollback <id>` and the Time
Travel bookmark becomes `--bookmark=<b>`. `reaches_wrangler` is what decides,
at the capture, whether that trip is still possible.

Its whole difficulty is that it must accept a surrogate and reject an
unencodable value, so the allow and deny directions are cases here. Saying no to the first would fail
deploys that work; saying yes to the second records a value the recovery path
cannot spend.
"""

from __future__ import annotations

import json
import pathlib
import sys
import unittest
from collections.abc import Sequence
from typing import override

MODULE = (
    pathlib.Path(__file__).resolve().parents[2] / "deploy-lib" / "wrangler_runner.py"
)

sys.path.insert(0, str(MODULE.parent))

import wrangler_runner
import wrangler_transient

# What wrangler_runner's own surrogateescape decode produces from a byte the
# Cloudflare API rendered that is not UTF-8.
DECODED_RAW_BYTE = b"\xff".decode("utf-8", errors="surrogateescape")

# What a document produces when it carries a lone surrogate as its own JSON
# escape. Written through json.loads rather than as a literal, because a `\u`
# escape in a source string is the character itself - a docstring holding one
# stops the module importing at all.
JSON_ESCAPED_SURROGATE = json.loads('"\\ud800"')


class ReachesWrangler(unittest.TestCase):
    def test_an_ordinary_version_id_and_bookmark_pass(self) -> None:
        for value in (
            "11111111-1111-1111-1111-111111111111",
            "00000000000000000000-0000000000000000-00000000-0000",
        ):
            with self.subTest(value=value):
                self.assertTrue(wrangler_runner.reaches_wrangler(value))

    def test_a_decoded_raw_byte_passes(self) -> None:
        # The reachable direction: argv
        # is encoded with surrogateescape too, so this round-trips to Cloudflare
        # as the byte it came from. A check that refused it - "is this ASCII", a
        # plain utf-8 encode - would fail deploys that work.
        self.assertTrue(wrangler_runner.reaches_wrangler(f"v{DECODED_RAW_BYTE}1"))

    def test_a_json_escaped_lone_surrogate_is_refused(self) -> None:
        # No encoder takes this one, so the value cannot become an argument at
        # all. Recorded rather than refused, it raises where it is spent, which
        # is inside the recovery path.
        self.assertFalse(wrangler_runner.reaches_wrangler(JSON_ESCAPED_SURROGATE))

    def test_non_ascii_text_is_not_what_this_refuses(self) -> None:
        # The predicate is about encodability, not about an alphabet. Guards
        # against it being "tightened" later into an ASCII check.
        self.assertTrue(wrangler_runner.reaches_wrangler("vers\u00e3o-1"))


NOT_FOUND = wrangler_runner.WranglerResult(
    returncode=1,
    stdout="",
    stderr="The requested Worker version could not be found. [code: 100146]\n",
)
FOUND = wrangler_runner.WranglerResult(returncode=0, stdout="{}", stderr="")


class ScriptedRunner(wrangler_runner.WranglerRunner):
    """Answers each attempt from a script instead of running wrangler."""

    def __init__(self, answers: list[wrangler_runner.WranglerResult]) -> None:
        super().__init__(wrangler=["wrangler"], backoff_seconds=[0, 0, 0])
        self._answers = answers
        self.attempts = 0

    @override
    def _invoke(self, argv: Sequence[str]) -> wrangler_runner.WranglerResult:
        self.attempts += 1
        return self._answers.pop(0)


class RetryAlso(unittest.TestCase):
    """A caller widening the roster for one call, as the runtime check's
    read-back of its own upload does for the version not-found."""

    def test_the_widened_error_is_retried_until_it_clears(self) -> None:
        # Guards the deploy that failed on a read-back a second after upload.
        runner = ScriptedRunner([NOT_FOUND, FOUND])
        result = runner.run(
            "versions_view", [], retry_also=wrangler_transient.is_version_not_found
        )
        self.assertTrue(result.ok)
        self.assertEqual(runner.attempts, 2)

    def test_the_widening_keeps_the_attempt_budget(self) -> None:
        # A version that never appears must still end the read, not hang it.
        runner = ScriptedRunner([NOT_FOUND] * 4)
        result = runner.run(
            "versions_view", [], retry_also=wrangler_transient.is_version_not_found
        )
        self.assertFalse(result.ok)
        self.assertEqual(runner.attempts, 4)

    def test_without_the_widening_it_is_not_retried(self) -> None:
        # Every other read of a version id takes not-found as the answer.
        runner = ScriptedRunner([NOT_FOUND, FOUND])
        self.assertFalse(runner.run("versions_view", []).ok)
        self.assertEqual(runner.attempts, 1)


if __name__ == "__main__":
    unittest.main()
