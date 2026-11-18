#!/usr/bin/env python3
"""Suite for the classification of one `_deploy_lock` read.

The answers, and the cases are grouped by which one is at stake. Reading
`locked` as unlocked walks a deploy through a lock somebody else is holding.
Reading unlocked as locked refuses a deploy that should have run. Reading
anything unmodeled as either - rather than as unreadable - is the one that
looks harmless and is not: the caller retries an unreadable state and then
refuses, so "unreadable" is the answer that keeps a lock nobody could read from
being reported as clear.
"""

from __future__ import annotations

import json
import pathlib
import sys
import unittest

MODULE = pathlib.Path(__file__).resolve().parents[2] / "deploy-lib" / "lock_state.py"

sys.path.insert(0, str(MODULE.parent))

import lock_state


def result_set(*values: object) -> str:
    """The envelope `wrangler d1 execute --json` returns for the lock SELECT."""
    return json.dumps([{"results": [{"locked": value} for value in values]}])


class LockCase(unittest.TestCase):
    """Shared base: the assertions every case is written in. No rows of its own."""

    def assertState(
        self, exit_code: int, stdout: str, expected: str, why: str, stderr: str = ""
    ) -> None:
        self.assertEqual(lock_state.classify(exit_code, stdout, stderr), expected, why)

    def assertUnreadable(
        self, exit_code: int, stdout: str, why: str, stderr: str = ""
    ) -> None:
        with self.assertRaises(lock_state.Unreadable, msg=why):
            lock_state.classify(exit_code, stdout, stderr)


class ReadableStates(LockCase):
    def test_a_one_row_is_locked(self) -> None:
        self.assertState(
            0, result_set(1), lock_state.LOCKED, "1 is the lock being held"
        )

    def test_a_zero_row_is_unlocked(self) -> None:
        self.assertState(
            0, result_set(0), lock_state.UNLOCKED, "0 is the lock being clear"
        )

    def test_the_same_values_as_strings_agree(self) -> None:
        # A driver that renders the column as text says the same thing.
        self.assertState(
            0, result_set("1"), lock_state.LOCKED, "a textual 1 is still locked"
        )
        self.assertState(
            0, result_set("0"), lock_state.UNLOCKED, "a textual 0 is still unlocked"
        )

    def test_no_row_at_all_is_unlocked(self) -> None:
        self.assertState(
            0,
            json.dumps([{"results": []}]),
            lock_state.UNLOCKED,
            "the table exists and nobody has ever taken the lock",
        )

    def test_only_the_first_row_decides(self) -> None:
        self.assertState(
            0, result_set(1, 0), lock_state.LOCKED, "the query selects one row by id"
        )

    def test_a_missing_lock_table_is_unlocked(self) -> None:
        # A database that has never carried the table has nobody holding it,
        # so a first-ever deploy has to be able to proceed.
        for text in (
            "no such table: _deploy_lock",
            "no such table: _deploy_lock in SELECT locked FROM _deploy_lock",
        ):
            with self.subTest(text=text):
                self.assertState(
                    1,
                    json.dumps({"error": {"text": text}}),
                    lock_state.UNLOCKED,
                    "the lock table not existing is a positive answer, not a gap",
                )

    def test_a_missing_lock_table_as_wrangler_4_reports_it_is_unlocked(self) -> None:
        # The document wrangler 4.127.1 printed for this read against a real D1
        # database with no tables, identifiers replaced. Its `text` is wrangler's
        # own sentence and the API's message is a note. Read as a gap, this
        # refused a first-ever deploy and would have refused a relocation into
        # an empty database at its first step.
        document = {
            "error": {
                "text": "A request to the Cloudflare API (/accounts/0123/d1/database/4567/query) failed.",
                "notes": [
                    {"text": "no such table: _deploy_lock: SQLITE_ERROR [code: 7500]"}
                ],
                "kind": "error",
                "name": "APIError",
                "code": 7500,
                "accountTag": "0123",
            }
        }
        self.assertState(
            1,
            json.dumps(document),
            lock_state.UNLOCKED,
            "the API's message is in a note; the table's absence is still the answer",
        )
        self.assertTrue(lock_state.names_missing_lock_table(json.dumps(document)))


class UnreadableStates(LockCase):
    """Every shape that must NOT resolve to either lock state."""

    def test_a_different_missing_table_is_not_our_answer(self) -> None:
        self.assertUnreadable(
            1,
            json.dumps({"error": {"text": "no such table: some_other_table"}}),
            "only the lock table's absence says anything about the lock",
        )

    def test_a_table_whose_name_only_begins_like_the_lock_table_is_not_it(
        self,
    ) -> None:
        # A prefix match reads `_deploy_lock_archive` missing as the lock table
        # missing, and answers "nobody holds the lock" about a database whose
        # lock table may be present and set.
        for name in ("_deploy_lock_archive", "_deploy_lock2", "_deploy_locks"):
            message = f"no such table: {name}: SQLITE_ERROR [code: 7500]"
            for error in (
                {"text": message},
                {"text": "failed.", "notes": [{"text": message}]},
            ):
                with self.subTest(error=error):
                    self.assertUnreadable(
                        1,
                        json.dumps({"error": error}),
                        "the table named has to be the lock table, not one like it",
                    )

    def test_a_note_about_another_table_or_in_another_shape_is_not_our_answer(
        self,
    ) -> None:
        # The notes are read for the API's message and nothing else, so neither
        # another table's absence nor the phrase somewhere unexpected counts.
        for error in (
            {
                "text": "failed.",
                "notes": [{"text": "no such table: users: SQLITE_ERROR"}],
            },
            {"text": "failed.", "notes": ["no such table: _deploy_lock"]},
            {"text": "failed.", "notes": {"text": "no such table: _deploy_lock"}},
            {"text": "failed.", "detail": "no such table: _deploy_lock"},
        ):
            with self.subTest(error=error):
                self.assertUnreadable(
                    1,
                    json.dumps({"error": error}),
                    "only the API's own message about the lock table is an answer",
                )

    def test_a_failed_read_with_any_other_error_is_unreadable(self) -> None:
        for stdout, stderr in (
            (json.dumps({"error": {"text": "authentication failed"}}), ""),
            (json.dumps({"error": {}}), ""),
            (json.dumps({}), ""),
            ("", "connection refused"),
            ("not json", "boom"),
        ):
            with self.subTest(stdout=stdout):
                self.assertUnreadable(
                    1, stdout, "a read that failed says nothing about the lock", stderr
                )

    def test_a_non_numeric_lock_value_is_unreadable(self) -> None:
        # true/false are the tempting ones: a boolean column is a plausible
        # future shape, and guessing which way it maps is how a deploy walks
        # through a held lock.
        values: tuple[object, ...] = (
            True,
            False,
            None,
            2,
            -1,
            1.0,
            "yes",
            "",
            [],
            {},
            "01",
            "10",
        )
        for value in values:
            with self.subTest(value=value):
                self.assertUnreadable(
                    0,
                    result_set(value),
                    "a value this module does not model is not a lock state",
                )

    def test_a_row_without_the_column_is_unreadable(self) -> None:
        self.assertUnreadable(
            0, json.dumps([{"results": [{}]}]), "no locked column is no answer"
        )

    def test_a_document_of_the_wrong_shape_is_unreadable(self) -> None:
        for stdout in (
            json.dumps([{"results": {}}]),
            json.dumps([{"results": None}]),
            json.dumps([{"results": [1]}]),
            json.dumps([{}]),
            json.dumps([]),
            json.dumps([1]),
            json.dumps({"results": [{"locked": 1}]}),
            "null",
            "not json",
            "",
        ):
            with self.subTest(stdout=stdout):
                self.assertUnreadable(
                    0, stdout, "an unexpected envelope is not a lock state"
                )

    def test_the_operator_payload_prefers_stdout_then_falls_back(self) -> None:
        # wrangler writes the API's own error document to stdout; stderr is
        # what a call that died before writing one leaves behind.
        with self.assertRaises(lock_state.Unreadable) as raised:
            lock_state.classify(1, '{"error":{"text":"nope"}}', "stderr text")
        self.assertIn("nope", str(raised.exception))
        with self.assertRaises(lock_state.Unreadable) as raised:
            lock_state.classify(1, "", "stderr text")
        self.assertEqual(str(raised.exception), "stderr text")


if __name__ == "__main__":
    unittest.main()
