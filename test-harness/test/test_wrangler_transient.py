#!/usr/bin/env python3
"""Suite for the transient-output roster that decides which wrangler failures
get another attempt.

The things are pinned here, and they fail for different reasons. The roster
rows are an allowlist, so a missing row is a deploy that aborts on a blip it
could have ridden out; the boundaries around a status are the opposite risk, so
a loose one retries output that names no status at all. Every case says whether
it guards the roster or token boundary.

The classifier is driven through its real executable, the same way a deploy
reaches it, so a decision this suite accepts is a decision the shell entry
point gets.
"""

from __future__ import annotations

import pathlib
import subprocess
import sys
import unittest

CLASSIFIER = (
    pathlib.Path(__file__).resolve().parents[2] / "deploy-lib" / "wrangler_transient.py"
)

sys.path.insert(0, str(CLASSIFIER.parent))

import wrangler_transient


class ClassifierCase(unittest.TestCase):
    """Shared base: drives the CLI and states whether a row fails classification or CLI rendering.

    Carries no test rows of its own, so the suites below inherit the helpers
    without inheriting each other's cases.
    """

    def classify(self, output: str) -> int:
        result = subprocess.run(
            [sys.executable, str(CLASSIFIER)],
            input=output.encode("utf-8", errors="surrogateescape"),
            capture_output=True,
            check=False,
        )
        return result.returncode

    def assertRetried(self, output: str, why: str) -> None:
        rc = self.classify(output)
        self.assertEqual(rc, 0, f"{why}: expected transient (rc 0), got rc {rc}")
        self.assertTrue(wrangler_transient.is_transient(output), why)

    def assertNotRetried(self, output: str, why: str) -> None:
        rc = self.classify(output)
        self.assertEqual(rc, 1, f"{why}: expected non-transient (rc 1), got rc {rc}")
        self.assertFalse(wrangler_transient.is_transient(output), why)


class TransportFailures(ClassifierCase):
    """A connection that failed, named by the runtime or by the proxy."""

    def test_node_errno_strings_retry(self) -> None:
        # Each names a socket that never delivered a response, so there is no
        # verdict to mistake one for.
        for errno in (
            "fetch failed",
            "EAI_AGAIN",
            "ECONNRESET",
            "ETIMEDOUT",
            "ENETUNREACH",
        ):
            with self.subTest(errno=errno):
                self.assertRetried(
                    f"[ERROR] {errno} while uploading version",
                    f"{errno} is a failed connection, not an answer",
                )

    def test_upstream_connect_error_retries(self) -> None:
        self.assertRetried(
            "upstream connect error or disconnect/reset before headers",
            "the proxy emits this only when it could not reach its upstream",
        )

    def test_auth_failure_is_an_answer_not_a_blip(self) -> None:
        self.assertNotRetried(
            "Authentication error [code: 10000]\nYou need to log in.",
            "retrying a credential problem burns the deploy window on a certain failure",
        )


class ApiRenderedFailures(ClassifierCase):
    """Shapes the API itself puts into words rather than into a status."""

    def test_d1_reset_and_overload_shapes_retry(self) -> None:
        for shape in (
            "code: 7429",
            "storage operation exceeded timeout",
            "Too Many Requests",
        ):
            with self.subTest(shape=shape):
                self.assertRetried(
                    f"A request to the Cloudflare API failed. {shape}",
                    f"{shape} is a control-plane blip a later attempt can find clear",
                )

    def test_match_is_case_sensitive(self) -> None:
        # The roster carries the casing the API renders. A case-insensitive
        # match would fold unrelated prose into the allowlist.
        self.assertNotRetried(
            "the server said too many requests were made",
            "lowercase prose is not the API's rendered shape",
        )


class EnvelopeRender(ClassifierCase):
    """`HTTP <status>`, printed after wrangler parses the JSON envelope."""

    def test_every_blessed_status_retries(self) -> None:
        for status in (429, 500, 502, 503, 504, 520, 521, 522, 523, 524):
            with self.subTest(status=status):
                self.assertRetried(
                    f"A request to the Cloudflare API failed. HTTP {status}",
                    f"HTTP {status} is a server-side failure a retry can outlast",
                )

    def test_client_error_statuses_do_not_retry(self) -> None:
        # 4xx other than 429 is the API's verdict on the request; another
        # identical attempt gets the same verdict.
        for status in (400, 401, 403, 404, 409):
            with self.subTest(status=status):
                self.assertNotRetried(
                    f"A request to the Cloudflare API failed. HTTP {status}",
                    f"HTTP {status} is an answer about the request, not about the moment",
                )


class EdgeRender(ClassifierCase):
    """The raw-exchange dump wrangler prints when the body did not parse.

    The header line says only that the BODY was unparseable, and the edge
    writes one over a 403 as readily as over a 503 - so these cases pin that
    the status inside the dump is what decides, never the header.
    """

    def test_status_in_the_dump_decides_not_the_header(self) -> None:
        header = "Received a malformed response from the API"
        self.assertRetried(
            f"{header}\nGET /accounts/x/workers -> 503 Service Unavailable",
            "an edge that cannot reach its backend is the blip retry exists for",
        )
        self.assertNotRetried(
            f"{header}\nGET /accounts/x/workers -> 403 Forbidden",
            "an unparseable body over a 4xx is still a verdict about the request",
        )

    def test_header_alone_is_not_enough(self) -> None:
        self.assertNotRetried(
            "Received a malformed response from the API",
            "the header names an unparseable body, which no status has been read from",
        )

    def test_every_edge_status_retries_with_and_without_a_reason_phrase(self) -> None:
        # Cloudflare's 52x family carries reason phrases this repo has never
        # seen printed, so the status has to fire without one.
        for status in (500, 502, 503, 504, 520, 521, 522, 523, 524):
            with self.subTest(status=status):
                self.assertRetried(
                    f"POST /accounts/x/scripts -> {status} Origin Down",
                    f"a {status} edge render with a reason phrase is transient",
                )
                self.assertRetried(
                    f"POST /accounts/x/scripts -> {status}",
                    f"a {status} at the very end of the output is still a {status}",
                )

    def test_digits_continuing_the_number_are_not_a_status(self) -> None:
        self.assertNotRetried(
            "POST /accounts/x/scripts -> 5200000 bytes transferred",
            "a longer number sharing a 520 prefix was never a 520",
        )

    def test_a_duration_is_not_a_status(self) -> None:
        self.assertNotRetried(
            "upload finished -> 520ms",
            "a letter continuing the token is how a duration renders",
        )

    def test_missing_arrow_is_not_an_edge_render(self) -> None:
        self.assertNotRetried(
            "the deploy returned 503 rows",
            "without the dump's arrow there is no status position to read",
        )


class BoundaryIsLocaleIndependent(ClassifierCase):
    """The status's right boundary is the ASCII whitespace set, on every host.

    A boundary written `\\s` would also match Unicode separators, making the
    verdict depend on the locale of whichever runner classified it - and CI
    hosts span C-locale Linux and UTF-8 macOS. These rows pin the ASCII
    reading so the same bytes get the same answer everywhere.
    """

    def test_each_ascii_whitespace_character_closes_the_status(self) -> None:
        for name, char in (
            ("space", " "),
            ("tab", "\t"),
            ("newline", "\n"),
            ("carriage return", "\r"),
            ("form feed", "\f"),
            ("vertical tab", "\v"),
        ):
            with self.subTest(boundary=name):
                self.assertRetried(
                    f"POST /x -> 522{char}Origin Down",
                    f"a status closed by a {name} is a status",
                )

    def test_a_unicode_separator_does_not_close_the_status(self) -> None:
        self.assertNotRetried(
            "POST /x -> 522\u00a0Origin Down",
            "a non-breaking space is not the ASCII separator the render writes",
        )

    def test_undecodable_bytes_do_not_manufacture_a_match(self) -> None:
        # Output arrives as arbitrary bytes; a lossy decode could fold an
        # invalid byte into a character the roster matches on.
        self.assertNotRetried(
            "POST /x -> 522\udcffrest",
            "an undecodable byte is not a boundary",
        )


class CliContract(ClassifierCase):
    """What the shell entry point depends on."""

    def test_empty_output_is_not_transient(self) -> None:
        self.assertNotRetried("", "no output names no error shape to retry on")

    def test_whole_stream_is_read_before_deciding(self) -> None:
        # The shell feeds this over a pipe. A reader that exited on its first
        # match would fault the writer, and under pipefail that fault becomes
        # the classifier's answer.
        payload = "fetch failed\n" + ("x" * 512 * 1024) + "\ntail\n"
        result = subprocess.run(
            [sys.executable, str(CLASSIFIER)],
            input=payload.encode(),
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stderr, b"")

    def test_arguments_are_a_usage_error_distinct_from_a_verdict(self) -> None:
        # rc 2 rather than 1: the caller has to tell "not transient" apart from
        # "the classifier never ran", or a broken invocation silently disables
        # every retry.
        result = subprocess.run(
            [sys.executable, str(CLASSIFIER), "fetch failed"],
            input=b"",
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn(b"stdin", result.stderr)


# The read-back of a version this run just uploaded, as a staging deploy saw it.
VERSION_NOT_FOUND = (
    "[ERROR] A request to the Cloudflare API (/accounts/<account>/workers/scripts/"
    "<script>/versions/<id>) failed.\n"
    "  The requested Worker version could not be found, please check the ID being "
    "passed and try again. [code: 100146]\n"
)


class VersionNotFound(ClassifierCase):
    """The version endpoint's not-found, transient only for a fresh upload."""

    def test_it_is_not_in_the_roster(self) -> None:
        # Guards the roster: for an id nobody just uploaded, not-found is the
        # answer, and the shell helper and every pipeline read share the roster.
        self.assertNotRetried(VERSION_NOT_FOUND, "a missing version is an answer")

    def test_the_opt_in_predicate_names_it(self) -> None:
        self.assertTrue(wrangler_transient.is_version_not_found(VERSION_NOT_FOUND))
        self.assertFalse(
            wrangler_transient.is_version_not_found(
                "[ERROR] HTTP 503 Service Unavailable"
            )
        )

    def test_promotion_matches_the_target_in_a_colored_api_error(self) -> None:
        error = VERSION_NOT_FOUND.replace("[ERROR]", "\x1b[31m[ERROR]\x1b[0m").replace(
            "failed.\n", "failed.\n\n"
        )
        self.assertTrue(wrangler_transient.missing_promotion_target(error, "<id>"))
        self.assertFalse(
            wrangler_transient.missing_promotion_target(error, "another-id")
        )

    def test_a_longer_code_is_not_this_one(self) -> None:
        # Guards the token boundary: digits continuing the code make it another
        # error, which the opt-in must not retry.
        self.assertFalse(wrangler_transient.is_version_not_found("[code: 1001460]"))


if __name__ == "__main__":
    unittest.main()
