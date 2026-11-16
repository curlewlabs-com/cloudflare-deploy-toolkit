#!/usr/bin/env python3
"""Suite for the commit a Worker version's upload message names.

The writer and the reader of that message are one module so they cannot drift,
and the round trip below is what holds them together: a reader that stopped
recognising what the upload writes would refuse every expected-build deploy,
and one that took a maintenance version, a foreign one, or a short SHA for a
commit would let a deploy go over a serving state nobody checked.
"""

from __future__ import annotations

import pathlib
import sys
import unittest

MODULE = (
    pathlib.Path(__file__).resolve().parents[2] / "deploy-lib" / "version_message.py"
)

sys.path.insert(0, str(MODULE.parent))

import version_message

COMMIT = "abc1234" + "0" * 33


def version(message: object) -> dict[str, object]:
    return {"annotations": {"workers/message": message}}


class FinalCommit(unittest.TestCase):
    def test_a_final_upload_names_its_full_commit_with_or_without_an_annotation(
        self,
    ) -> None:
        for annotation in ("", "gen=34"):
            with self.subTest(annotation=annotation):
                message = version_message.upload_message(
                    version_message.FINAL, "abc1234", COMMIT, annotation
                )
                self.assertEqual(version_message.final_commit(version(message)), COMMIT)

    def test_a_short_sha_alone_names_no_commit(self) -> None:
        # Distinct commits share short prefixes, so a version that recorded
        # only the short form - every one uploaded before the full commit was
        # written - cannot be told apart from a rolled-back one that shares it.
        for annotation in ("", "gen=34"):
            with self.subTest(annotation=annotation):
                message = version_message.upload_message(
                    version_message.FINAL, "abc1234", "", annotation
                )
                self.assertIsNone(version_message.final_commit(version(message)))

    def test_a_maintenance_upload_names_no_commit(self) -> None:
        message = version_message.upload_message(
            version_message.MAINTENANCE, "abc1234", COMMIT, "gen=34"
        )
        self.assertIsNone(version_message.final_commit(version(message)))

    def test_anything_not_a_final_upload_message_names_no_commit(self) -> None:
        # A version uploaded outside the pipeline carries whatever message its
        # uploader typed, or none, and the document may not be a version.
        documents: tuple[object, ...] = (
            version(f"hotfix from laptop commit={COMMIT}"),
            version(f"deploy-pipeline final (abc1234) commit={COMMIT.upper()}"),
            version(f"deploy-pipeline final (abc1234) commit={COMMIT[:39]}"),
            version(f"deploy-pipeline final (abc1234) commit={COMMIT}trailing"),
            version(None),
            {"annotations": None},
            [],
            None,
        )
        for document in documents:
            with self.subTest(document=document):
                self.assertIsNone(version_message.final_commit(document))


if __name__ == "__main__":
    unittest.main()
