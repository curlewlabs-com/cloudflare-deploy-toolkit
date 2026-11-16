#!/usr/bin/env python3
"""Suite for the bookmark read - the one D1 decision with no other coverage.

The rest of deploy_database.py is orchestration around wrangler calls, and the
bats suites drive every arm of it against the mock: the lock read-back branches,
the unlock's locked, unlocked, and unreadable answers, and the restore. This is the decision they cannot reach,
because the mock returns one bookmark document and a deploy only ever sees the
shape Cloudflare sends.

What it decides has a consequence: the bookmark is the only way out of a
maintenance window. A value the pipeline accepts and cannot restore from opens
the window with no exit, so anything but a non-empty string refuses and the
deploy stops before the migration runs.
"""

from __future__ import annotations

import pathlib
import sys
import unittest

MODULE = (
    pathlib.Path(__file__).resolve().parents[2] / "deploy-lib" / "deploy_database.py"
)

sys.path.insert(0, str(MODULE.parent))

import deploy_database


class BookmarkRead(unittest.TestCase):
    def test_a_real_document_yields_the_bookmark(self) -> None:
        self.assertEqual(
            deploy_database.bookmark_from(
                '{"bookmark":"00000000000000000000-0000000000000000"}'
            ),
            "00000000000000000000-0000000000000000",
        )

    def test_extra_fields_do_not_disturb_the_read(self) -> None:
        self.assertEqual(
            deploy_database.bookmark_from('{"bookmark":"abc","database":"db"}'), "abc"
        )

    def test_every_shape_that_is_not_a_bookmark_refuses(self) -> None:
        # Affirmative only, the same discipline the other ported decisions
        # follow. A number reads as a bookmark through `jq -r` and is not one;
        # an empty string is the value a missing key renders as.
        for document in (
            '{"bookmark":null}',
            '{"bookmark":123}',
            '{"bookmark":""}',
            '{"bookmark":{"id":"abc"}}',
            '{"database":"db"}',
            "[]",
            '"abc"',
            "null",
            "not json",
            "",
        ):
            with self.subTest(document=document):
                self.assertEqual(deploy_database.bookmark_from(document), "")


if __name__ == "__main__":
    unittest.main()
