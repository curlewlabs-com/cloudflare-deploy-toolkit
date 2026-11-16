#!/usr/bin/env python3
"""Deployment-verifier tests.

These cover the consequence-heavy branches: malformed and zone-less timestamps,
accepted clock skew, rejected future timestamps, stale deployments, and the
optional live-SHA assertion. A regression here turns a successful deploy into a
false-negative workflow failure, or lets a sibling deploy's fresh timestamp stand
in for the build that was actually pushed.

No network: VERIFY_DEPLOYMENT_RESPONSE and VERIFY_DEPLOYMENT_SHA_RESPONSE supply
the status and SHA bodies, and VERIFY_DEPLOYMENT_NOW_EPOCH pins "now" so every age assertion
is a fixed arithmetic fact rather than a clock read.

The parser cases below carry the property the verifier's safety rests on: a timestamp
the verifier cannot read, or can only read by guessing a zone, fails closed
rather than reaching the age arithmetic.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import unittest
from pathlib import Path

BIN_DIR = Path(__file__).resolve().parents[2] / "bin"
sys.path.insert(0, str(BIN_DIR))

from verify_deployment import (
    VerifyFailure,
    latest_deploy_timestamp,
    parse_deploy_epoch,
)

SCRIPT = BIN_DIR / "verify_deployment.py"

# 2026-04-22T22:33:00Z. Every timestamp below is placed relative to it, so the
# age each case asserts is arithmetic rather than a reading of the host clock.
NOW_EPOCH = "1776897180"
FRESH_RESPONSE = '{"result":{"deployments":[{"created_on":"2026-04-22T22:32:00Z"}]}}'


def deployments(created_on: str) -> str:
    return f'{{"result":{{"deployments":[{{"created_on":"{created_on}"}}]}}}}'


class TimestampParsing(unittest.TestCase):
    def test_utc_designator_and_explicit_offset_agree(self) -> None:
        self.assertEqual(parse_deploy_epoch("2026-04-22T22:32:00Z"), 1776897120)
        self.assertEqual(parse_deploy_epoch("2026-04-22T22:32:00+00:00"), 1776897120)
        # A non-UTC offset is not re-read as UTC; the instant is what matters.
        self.assertEqual(parse_deploy_epoch("2026-04-22T23:32:00+01:00"), 1776897120)

    def test_fractional_seconds_are_accepted(self) -> None:
        self.assertEqual(parse_deploy_epoch("2026-04-22T22:32:00.512Z"), 1776897120)

    def test_a_zoneless_timestamp_is_refused(self) -> None:
        """A reader that guesses the zone is wrong by hours, silently, and turns
        a good deploy red. Refusing is the only answer that cannot guess."""
        with self.assertRaises(VerifyFailure) as raised:
            parse_deploy_epoch("2026-04-22T22:32:00")
        self.assertIn("carries no UTC offset", str(raised.exception))

    def test_impossible_dates_raise_rather_than_passing_a_range_check(self) -> None:
        for value in (
            "2026-02-31T00:00:00Z",
            "2026-13-01T00:00:00Z",
            "not-a-timestamp",
            "",
        ):
            with self.subTest(value=value), self.assertRaises(VerifyFailure):
                parse_deploy_epoch(value)

    def test_the_runner_zone_cannot_reach_the_epoch(self) -> None:
        """The zones are chosen to make a zone-leaking parser visible: under
        Kiritimati (UTC+14) the local DATE is already 2026-04-23 while under
        Niue (UTC-11) it is still 2026-04-22, so a reader that let the host zone
        in returns zone-dependent epochs for the one instant below."""
        for zone in (
            "UTC",
            "Pacific/Kiritimati",
            "Pacific/Niue",
            "America/Los_Angeles",
        ):
            with self.subTest(zone=zone):
                previous = os.environ.get("TZ")
                os.environ["TZ"] = zone
                time.tzset()
                try:
                    self.assertEqual(
                        parse_deploy_epoch("2026-04-22T22:32:00Z"), 1776897120
                    )
                finally:
                    if previous is None:
                        del os.environ["TZ"]
                    else:
                        os.environ["TZ"] = previous
                    time.tzset()


class DeploymentsEnvelope(unittest.TestCase):
    def test_newest_created_on_is_read(self) -> None:
        self.assertEqual(
            latest_deploy_timestamp(FRESH_RESPONSE, "test-worker"),
            "2026-04-22T22:32:00Z",
        )

    def test_every_unreadable_shape_is_the_same_refusal(self) -> None:
        """A missing field, a wrong runtime type, and an unparseable document all
        mean the same thing to the caller: nothing here says a deploy landed."""
        for document in (
            "{}",
            '{"result":{}}',
            '{"result":{"deployments":[]}}',
            '{"result":{"deployments":{}}}',
            '{"result":{"deployments":[{}]}}',
            '{"result":{"deployments":[{"created_on":null}]}}',
            '{"result":{"deployments":[{"created_on":123}]}}',
            '{"result":{"deployments":["not-an-object"]}}',
            "<html>challenge</html>",
            "",
        ):
            with self.subTest(document=document):
                with self.assertRaises(VerifyFailure) as raised:
                    latest_deploy_timestamp(document, "test-worker")
                self.assertIn(
                    "No deployments found for test-worker", str(raised.exception)
                )


class VerifierRunner:
    """Shared invocation of the real executable, held off the TestCase so the
    timestamp and SHA suites can use it without inheriting each other's rows."""

    def run_verifier(self, **overrides: str) -> subprocess.CompletedProcess[str]:
        env = {
            "PATH": os.environ["PATH"],
            "CLOUDFLARE_API_TOKEN": "test-token",
            "CLOUDFLARE_ACCOUNT_ID": "test-account",
            "SCRIPT_NAME": "test-worker",
            "VERIFY_DEPLOYMENT_NOW_EPOCH": NOW_EPOCH,
        }
        env.update(overrides)
        return subprocess.run(
            [str(SCRIPT)], env=env, capture_output=True, text=True, check=False
        )


class TimestampGate(VerifierRunner, unittest.TestCase):
    """End-to-end through the real executable, which is where the exit code, the
    workflow-command rendering, and the age arithmetic live."""

    def test_fresh_deploy_passes(self) -> None:
        done = self.run_verifier(VERIFY_DEPLOYMENT_RESPONSE=FRESH_RESPONSE)
        self.assertEqual(done.returncode, 0, msg=done.stdout + done.stderr)
        self.assertIn(
            "Latest deployment for test-worker: 2026-04-22T22:32:00Z (60s ago)",
            done.stdout,
        )

    def test_malformed_timestamp_fails_explicitly(self) -> None:
        """API shape drift must be diagnosable, not silently arithmetic."""
        done = self.run_verifier(
            VERIFY_DEPLOYMENT_RESPONSE=deployments("not-a-timestamp")
        )
        self.assertEqual(done.returncode, 1)
        self.assertIn(
            "Could not parse deployment timestamp: not-a-timestamp", done.stderr
        )

    def test_small_future_skew_is_tolerated(self) -> None:
        """Distributed clocks disagree by seconds; a good deploy must not fail
        for it."""
        done = self.run_verifier(
            VERIFY_DEPLOYMENT_RESPONSE=deployments("2026-04-22T22:33:30Z")
        )
        self.assertEqual(done.returncode, 0, msg=done.stdout + done.stderr)
        self.assertIn("treating as clock skew", done.stdout)
        self.assertIn(
            "Latest deployment for test-worker: 2026-04-22T22:33:30Z (0s ago)",
            done.stdout,
        )

    def test_large_future_skew_is_refused(self) -> None:
        """A timestamp this far ahead cannot belong to the deploy just run."""
        done = self.run_verifier(
            VERIFY_DEPLOYMENT_RESPONSE=deployments("2026-04-22T22:34:30Z")
        )
        self.assertEqual(done.returncode, 1)
        self.assertIn("Latest deployment timestamp is 90s in the future", done.stdout)

    def test_stale_deployment_is_refused(self) -> None:
        done = self.run_verifier(
            VERIFY_DEPLOYMENT_RESPONSE=deployments("2026-04-22T22:20:00Z")
        )
        self.assertEqual(done.returncode, 1)
        self.assertIn("Latest deployment is 780s old", done.stdout)

    def test_a_non_numeric_knob_is_refused_rather_than_defaulted(self) -> None:
        """Falling back to the default would run the freshness check under a
        window the operator did not ask for and did not know was ignored."""
        done = self.run_verifier(
            VERIFY_DEPLOYMENT_RESPONSE=FRESH_RESPONSE,
            VERIFY_DEPLOYMENT_MAX_AGE_SECONDS="five minutes",
        )
        self.assertEqual(done.returncode, 1)
        self.assertIn(
            "VERIFY_DEPLOYMENT_MAX_AGE_SECONDS must be an integer", done.stderr
        )

    def test_missing_required_env_is_named(self) -> None:
        for missing in ("CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID", "SCRIPT_NAME"):
            with self.subTest(missing=missing):
                done = self.run_verifier(
                    VERIFY_DEPLOYMENT_RESPONSE=FRESH_RESPONSE, **{missing: ""}
                )
                self.assertEqual(done.returncode, 1)
                self.assertIn(f"::error::{missing} is required", done.stderr)


class LiveShaAssertion(VerifierRunner, unittest.TestCase):
    """The timestamp gate proves *a* deployment is recent; these rows are what
    proves *this* build is the one serving."""

    def run_sha(
        self, expected: str, body: str, attempts: str = "2", **overrides: str
    ) -> subprocess.CompletedProcess[str]:
        return self.run_verifier(
            VERIFY_DEPLOYMENT_RESPONSE=FRESH_RESPONSE,
            VERIFY_DEPLOYMENT_EXPECTED_SHA=expected,
            VERIFY_DEPLOYMENT_SHA_URL="https://example.test/v1/health",
            VERIFY_DEPLOYMENT_SHA_RESPONSE=body,
            VERIFY_DEPLOYMENT_SHA_ATTEMPTS=attempts,
            VERIFY_DEPLOYMENT_SHA_INTERVAL_SECONDS="0",
            **overrides,
        )

    def test_matching_sha_passes(self) -> None:
        done = self.run_sha("abc1234", '{"sha":"abc1234","status":"ok"}')
        self.assertEqual(done.returncode, 0, msg=done.stdout + done.stderr)
        self.assertIn("serving expected SHA abc1234", done.stdout)

    def test_sibling_deploy_race_fails_closed(self) -> None:
        """Fresh timestamp, different SHA live: trusting the timestamp here is
        exactly the hole the SHA assertion exists to close."""
        done = self.run_sha("abc1234", '{"sha":"deadbee","status":"ok"}')
        self.assertEqual(done.returncode, 1)
        self.assertIn("did not report expected SHA abc1234", done.stdout)

    def test_non_json_body_fails_closed_with_a_truncated_snippet(self) -> None:
        """A WAF or bot-challenge page means the request never reached the
        Worker; the snippet is what makes that diagnosable rather than opaque."""
        done = self.run_sha("abc1234", "<html>challenge</html>", attempts="1")
        self.assertEqual(done.returncode, 1)
        self.assertIn("did not report expected SHA abc1234", done.stdout)
        self.assertIn("challenge", done.stdout)

    def test_readiness_filter_rejects_a_matching_but_unready_worker(self) -> None:
        """A windowed deploy's maintenance and final versions share one SHA, so
        the SHA alone cannot tell them apart - the jq filter is what does."""
        ready_filter = (
            "select(.maintenanceMode == false and .deployLocked == false) | .sha"
        )
        for body, ready in (
            ('{"sha":"abc1234","maintenanceMode":true,"deployLocked":false}', False),
            ('{"sha":"abc1234","maintenanceMode":false,"deployLocked":true}', False),
            ('{"sha":"abc1234","maintenanceMode":false,"deployLocked":false}', True),
        ):
            with self.subTest(body=body):
                done = self.run_sha(
                    "abc1234",
                    body,
                    attempts="1",
                    VERIFY_DEPLOYMENT_SHA_JQ=ready_filter,
                )
                self.assertEqual(done.returncode, 0 if ready else 1)

    def test_nested_sha_field_is_reachable(self) -> None:
        done = self.run_sha(
            "abc1234",
            '{"build":{"sha":"abc1234"}}',
            attempts="1",
            VERIFY_DEPLOYMENT_SHA_JQ=".build.sha",
        )
        self.assertEqual(done.returncode, 0, msg=done.stdout + done.stderr)
        self.assertIn("serving expected SHA abc1234", done.stdout)

    def test_expected_sha_without_a_url_is_a_loud_misconfiguration(self) -> None:
        """Silently skipping the liveness assertion would leave the workflow
        green while checking nothing it asked for."""
        done = self.run_verifier(
            VERIFY_DEPLOYMENT_RESPONSE=FRESH_RESPONSE,
            VERIFY_DEPLOYMENT_EXPECTED_SHA="abc1234",
        )
        self.assertEqual(done.returncode, 1)
        self.assertIn("VERIFY_DEPLOYMENT_SHA_URL is not", done.stderr)

    def test_opting_out_leaves_timestamp_only_behavior_untouched(self) -> None:
        """A deploy with no SHA-bearing endpoint must be unaffected."""
        done = self.run_verifier(VERIFY_DEPLOYMENT_RESPONSE=FRESH_RESPONSE)
        self.assertEqual(done.returncode, 0, msg=done.stdout + done.stderr)
        self.assertIn("Latest deployment for test-worker", done.stdout)
        self.assertNotIn("expected SHA", done.stdout)


if __name__ == "__main__":
    unittest.main()
