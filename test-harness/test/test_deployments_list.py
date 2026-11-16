#!/usr/bin/env python3
"""Suite for the bootstrap and serving-version decisions the pipeline makes
about a `deployments list --json` document.

What is at stake in each is different, and every case says which. The serving
version becomes the rollback target and the reference the pre-promote and
post-promote TOCTOU re-checks
match, so accepting a shape that is not one-version-at-100 means a rollback
that restores the wrong code, or a serving-set change mid-deploy that nothing
notices. The bootstrap shape is the one emptiness a deploy may continue
through, so widening it means a deploy proceeding with no rollback target on
evidence nobody checked.
"""

from __future__ import annotations

import json
import pathlib
import sys
import unittest

MODULE = (
    pathlib.Path(__file__).resolve().parents[2] / "deploy-lib" / "deployments_list.py"
)

sys.path.insert(0, str(MODULE.parent))

import deployments_list


def one_version(version_id: str = "v-1", percentage: object = 100) -> str:
    """The shape a healthy single-version deployment has."""
    return json.dumps(
        [{"versions": [{"version_id": version_id, "percentage": percentage}]}]
    )


class DecisionCase(unittest.TestCase):
    """Shared base: the assertions every case is written in. No rows of its own."""

    def assertServes(self, document: str, version_id: str, why: str) -> None:
        self.assertEqual(
            deployments_list.single_serving_version_id(document), version_id, why
        )

    def assertRefuses(self, document: str, detail: str, why: str) -> None:
        with self.assertRaises(deployments_list.Refused, msg=why) as raised:
            deployments_list.single_serving_version_id(document)
        self.assertIn(detail, str(raised.exception), why)


class ServingVersion(DecisionCase):
    def test_one_version_at_100_is_the_serving_version(self) -> None:
        self.assertServes(
            one_version("abc-123"), "abc-123", "the healthy shape resolves"
        )

    def test_the_newest_deployment_is_the_one_read(self) -> None:
        document = json.dumps(
            [
                {"versions": [{"version_id": "older", "percentage": 100}]},
                {"versions": [{"version_id": "newest", "percentage": 100}]},
            ]
        )
        self.assertServes(
            document, "newest", "reading an older entry rolls back to stale code"
        )

    def test_a_gradual_split_refuses(self) -> None:
        document = json.dumps(
            [
                {
                    "versions": [
                        {"version_id": "a", "percentage": 50},
                        {"version_id": "b", "percentage": 50},
                    ]
                }
            ]
        )
        self.assertRefuses(
            document,
            "expected exactly one version",
            "picking either half of a split records a rollback target that was never serving alone",
        )

    def test_a_partial_percentage_refuses(self) -> None:
        self.assertRefuses(
            one_version("a", 90),
            "not serving at percentage 100",
            "a version at 90 percent is a rollout in progress, not the serving set",
        )

    def test_a_percentage_that_is_not_a_number_refuses(self) -> None:
        for percentage in ("100", True, None, [100]):
            with self.subTest(percentage=percentage):
                self.assertRefuses(
                    one_version("a", percentage),
                    "not serving at percentage 100",
                    "a non-numeric percentage is an unmodeled shape, not a hundred",
                )

    def test_a_float_hundred_is_still_a_hundred(self) -> None:
        # wrangler renders it as a JSON number; 100.0 and 100 are the same
        # number, and refusing one would refuse a healthy deploy.
        self.assertServes(one_version("a", 100.0), "a", "100.0 is 100")

    def test_a_missing_or_unusable_version_id_refuses(self) -> None:
        for entry in (
            {"percentage": 100},
            {"version_id": "", "percentage": 100},
            {"version_id": 123, "percentage": 100},
            {"version_id": None, "percentage": 100},
        ):
            with self.subTest(entry=entry):
                self.assertRefuses(
                    json.dumps([{"versions": [entry]}]),
                    "no usable version_id",
                    "an id the rollback cannot name is not a rollback target",
                )

    def test_an_empty_or_non_array_document_refuses(self) -> None:
        for document in ("[]", "{}", "null", '"abc"', "42"):
            with self.subTest(document=document):
                self.assertRefuses(
                    document,
                    "not a non-empty array",
                    "there is no newest deployment to read",
                )

    def test_a_malformed_document_refuses_without_naming_a_version(self) -> None:
        for document in ("not json", "", "[{", "[1,]"):
            with self.subTest(document=document):
                self.assertRefuses(
                    document,
                    "not valid JSON",
                    "an unparseable document is not evidence about the serving set",
                )

    def test_a_versions_field_that_is_not_an_array_refuses(self) -> None:
        # Including the string case, whose length is not a version count.
        for versions in ("abcdefgh", None, 3, {"version_id": "a"}):
            with self.subTest(versions=versions):
                self.assertRefuses(
                    json.dumps([{"versions": versions}]),
                    "no versions array",
                    "a versions field of the wrong type says nothing about how many versions serve",
                )

    def test_a_newest_entry_that_is_not_an_object_refuses(self) -> None:
        newest: object
        for newest in (1, "abc", [], None):
            with self.subTest(newest=newest):
                self.assertRefuses(
                    json.dumps([newest]),
                    "no versions array",
                    "a deployment entry of the wrong type carries no versions to read",
                )


class BootstrapShape(DecisionCase):
    """The only emptiness a deploy is allowed to continue through."""

    def assertBootstrap(self, document: str, expected: bool, why: str) -> None:
        self.assertIs(deployments_list.is_bootstrap_shape(document), expected, why)

    def test_no_deployments_at_all_is_bootstrap(self) -> None:
        self.assertBootstrap(
            "[]", True, "a Worker that has never deployed has no rollback target"
        )

    def test_a_newest_deployment_with_no_versions_is_bootstrap(self) -> None:
        self.assertBootstrap(
            json.dumps([{"versions": []}]),
            True,
            "an empty versions array is the same emptiness",
        )
        self.assertBootstrap(
            json.dumps(
                [
                    {"versions": [{"version_id": "a", "percentage": 100}]},
                    {"versions": []},
                ]
            ),
            True,
            "the newest entry is the one that decides",
        )

    def test_a_serving_version_is_not_bootstrap(self) -> None:
        self.assertBootstrap(
            one_version(), False, "a version is serving, so there is a target"
        )

    def test_a_split_is_not_bootstrap(self) -> None:
        document = json.dumps(
            [
                {
                    "versions": [
                        {"version_id": "a", "percentage": 50},
                        {"version_id": "b", "percentage": 50},
                    ]
                }
            ]
        )
        self.assertBootstrap(
            document,
            False,
            "a split must reach the extractor's refusal, not be waved through",
        )

    def test_an_unreadable_versions_field_is_not_bootstrap(self) -> None:
        # If any unreadable shape counted as
        # bootstrap, a deploy would continue with no rollback target on
        # evidence nobody checked.
        for document in (
            '[{"versions":null}]',
            "[{}]",
            '[{"versions":"abc"}]',
            "[1]",
            "[null]",
        ):
            with self.subTest(document=document):
                self.assertBootstrap(
                    document, False, "unreadable is not the same as empty"
                )

    def test_a_malformed_document_is_not_bootstrap(self) -> None:
        for document in ("not json", "", "{}", "null"):
            with self.subTest(document=document):
                self.assertBootstrap(
                    document, False, "an unparseable document proves no emptiness"
                )


if __name__ == "__main__":
    unittest.main()
