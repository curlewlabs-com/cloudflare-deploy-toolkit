#!/usr/bin/env python3
"""Suite for the runtime a Worker version records and the one a consumer expects.

The deploy refuses an upload whose runtime differs from the expected one, so the
risks run in each direction. A reader that took a malformed document for some default
runtime would pass a version nobody compared; one that refused a document
Cloudflare's schema allows - the omitted flags field above all - would stop
every deploy of a Worker that sets no flags.
"""

from __future__ import annotations

import pathlib
import sys
import unittest

MODULE = (
    pathlib.Path(__file__).resolve().parents[2] / "deploy-lib" / "version_runtime.py"
)

sys.path.insert(0, str(MODULE.parent))

import version_runtime


def version(script_runtime: object) -> dict[str, object]:
    return {"id": "v1", "resources": {"script_runtime": script_runtime}}


class OfVersion(unittest.TestCase):
    def test_an_omitted_flags_field_is_no_flags(self) -> None:
        # The shape a version with no flags reads back in: Cloudflare omits the
        # field when the list is empty.
        self.assertEqual(
            version_runtime.of_version(
                version({"compatibility_date": "2025-01-01", "usage_model": "standard"})
            ),
            version_runtime.Runtime("2025-01-01", frozenset()),
        )

    def test_flags_compare_as_a_set(self) -> None:
        runtime = version_runtime.of_version(
            version(
                {
                    "compatibility_date": "2025-01-01",
                    "compatibility_flags": ["b_flag", "a_flag"],
                }
            )
        )
        self.assertEqual(
            runtime,
            version_runtime.Runtime("2025-01-01", frozenset({"a_flag", "b_flag"})),
        )
        self.assertEqual(
            runtime.describe(),
            "compatibility_date 2025-01-01, compatibility_flags [a_flag, b_flag]",
        )

    def test_the_schema_examples_midnight_timestamp_is_the_same_day(self) -> None:
        self.assertEqual(
            version_runtime.of_version(
                version({"compatibility_date": "2022-11-08T00:00:00Z"})
            ).compatibility_date,
            "2022-11-08",
        )

    def test_a_document_that_states_no_runtime_is_unreadable(self) -> None:
        # Each of these would otherwise be read as some runtime nobody stated.
        documents: tuple[object, ...] = (
            None,
            {},
            {"resources": {}},
            version(None),
            version({}),
            version({"compatibility_date": "2025-02-31"}),
            version({"compatibility_date": "2025-01-01T12:00:00Z"}),
            version({"compatibility_date": 20250101}),
            version({"compatibility_date": "2025-01-01", "compatibility_flags": None}),
            version({"compatibility_date": "2025-01-01", "compatibility_flags": [1]}),
        )
        for document in documents:
            with (
                self.subTest(document=document),
                self.assertRaises(version_runtime.Unreadable),
            ):
                version_runtime.of_version(document)


class Expected(unittest.TestCase):
    def test_the_consumer_document_parses(self) -> None:
        self.assertEqual(
            version_runtime.expected(
                '{"compatibility_date": "2026-04-21", "compatibility_flags": ["x"]}'
            ),
            version_runtime.Runtime("2026-04-21", frozenset({"x"})),
        )

    def test_anything_else_is_unreadable(self) -> None:
        # Refused at startup: a deploy told to check against a runtime it
        # cannot read must not run as though it had not been told.
        for text in (
            "",
            "not json",
            "[]",
            '{"compatibility_date": "2026-04-21"}',
            '{"compatibility_date": "2026-04-21", "compatibility_flags": [], "x": 1}',
            '{"compatibility_date": "21/04/2026", "compatibility_flags": []}',
        ):
            with (
                self.subTest(text=text),
                self.assertRaises(version_runtime.Unreadable),
            ):
                version_runtime.expected(text)


if __name__ == "__main__":
    unittest.main()
