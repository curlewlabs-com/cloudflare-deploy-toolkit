#!/usr/bin/env python3
"""Verify that the latest Cloudflare Worker deployment is the one just pushed.

This lives in the toolkit because the reference workflow and the consumer
workflow need the same timestamp parsing and skew handling, and a verifier that
resolves Cloudflare's UTC timestamp against the runner's own zone turns a good
deploy red - the defect the offset requirement below exists to refuse.

The timestamp check alone proves *a* deployment is recent, not that *this* build
is live: the deployments list is "latest", not "live", so a sibling deploy
landing between our promote and this verify yields a fresh timestamp for a
different or older build and the check still passes. When the caller opts in
(VERIFY_DEPLOYMENT_EXPECTED_SHA + VERIFY_DEPLOYMENT_SHA_URL), this script
additionally polls a URL that echoes the running build's commit SHA and fails
closed unless it matches the expected commit. Workers without such an endpoint
(e.g. a static site) leave those vars unset and keep the timestamp-only behavior.

The spawned binaries, each for a reason `json`/`urllib` does not cover:

  curl  makes the deployments API and optional live-SHA HTTP reads, under policies that differ because the reads do.
        The deployments API read is `-sf --retry 5 --retry-connrefused`: curl's
        own exponential backoff over HTTP 408/429/5xx, timeouts, and a refused
        connection, a policy this would otherwise re-implement inside a deploy
        verifier, where getting it subtly wrong turns a good deploy into a red
        workflow. The live-SHA read carries no retry flags and no `-f` - its
        retries are this file's own bounded poll, which has to re-read a
        CHANGING value rather than the same one, and a non-2xx body is the
        mismatch diagnostic rather than something to fail on.
  jq    reads VERIFY_DEPLOYMENT_SHA_JQ, which is published as a jq *filter* and
        is used as one: a service deploy can pass
        `select(.maintenanceMode == false and .deployLocked == false) | .sha`, a
        readiness predicate no dotted-path reader can express. Every other JSON
        read here is json.loads.

Class-Audit, the parsing classes this file carries:

  Timestamp (Cloudflare `created_on`). Round-trip through the canonical parser
  rather than a shape regex, so an impossible date raises instead of passing a
  range check. An offset is REQUIRED: a naive timestamp is refused rather than
  assumed UTC, because a reader that guesses the zone is wrong whichever way it
  guesses, and the header's defect is what that costs.
  Whitespace-padded, empty, and non-string values all fail closed through the
  same parser.

  Live-SHA body. Untyped-object boundary: the body is whatever the URL returned,
  including a WAF challenge page, so it is never parsed here - jq reads it and
  anything that is not a bare matching string collapses to no match. Comparison
  is whole-string equality against a non-empty expected SHA, so a null, absent,
  multi-line, or filtered-out result cannot match.

  Deployments envelope. Read with json.loads and narrowed per field; a missing
  `.result.deployments[0].created_on`, a non-list, or an unparseable document is
  the same "no deployments found" refusal rather than a crash.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from datetime import datetime

API_BASE = "https://api.cloudflare.com/client/v4"
MAX_AGE_SECONDS = 300
MAX_FUTURE_SKEW_SECONDS = 60
DEFAULT_SHA_JQ = ".sha"
DEFAULT_SHA_ATTEMPTS = 6
DEFAULT_SHA_INTERVAL_SECONDS = 5

# Enough of a non-JSON body to tell a bot challenge or a 429 page apart from a
# real SHA, without letting a full HTML page flood the workflow log.
BODY_SNIPPET_CHARS = 200

REQUIRED_ENV = ("CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID", "SCRIPT_NAME")


class VerifyFailure(Exception):
    """A refusal whose message is already rendered for the workflow log."""

    def __init__(self, message: str, *, to_stderr: bool = False) -> None:
        super().__init__(message)
        self.to_stderr = to_stderr


def _int_env(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name, "")
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise VerifyFailure(
            f"::error::{name} must be an integer number of seconds, got '{raw}'",
            to_stderr=True,
        ) from None


def parse_deploy_epoch(timestamp: str) -> int:
    """Return the UTC epoch seconds for a Cloudflare `created_on` value.

    Raises VerifyFailure on anything this cannot prove it read correctly,
    including a timestamp carrying no UTC offset - see the Class-Audit note.
    """
    try:
        parsed = datetime.fromisoformat(timestamp.strip())
    except ValueError, TypeError:
        raise VerifyFailure(
            f"::error::Could not parse deployment timestamp: {timestamp}",
            to_stderr=True,
        ) from None
    if parsed.tzinfo is None:
        raise VerifyFailure(
            f"::error::Deployment timestamp carries no UTC offset, so its zone is a guess: {timestamp}",
            to_stderr=True,
        )
    return int(parsed.timestamp())


def latest_deploy_timestamp(document: str, script_name: str) -> str:
    """Pull `.result.deployments[0].created_on` out of the deployments envelope."""
    try:
        payload: object = json.loads(document)
    except json.JSONDecodeError:
        payload = None

    created_on: object = None
    if isinstance(payload, dict):
        result = payload.get("result")
        if isinstance(result, dict):
            deployments = result.get("deployments")
            if isinstance(deployments, list) and deployments:
                newest = deployments[0]
                if isinstance(newest, dict):
                    created_on = newest.get("created_on")

    if not isinstance(created_on, str) or not created_on:
        raise VerifyFailure(f"::error::No deployments found for {script_name}")
    return created_on


def _fetch_deployments(env: Mapping[str, str]) -> str:
    url = (
        f"{API_BASE}/accounts/{env['CLOUDFLARE_ACCOUNT_ID']}"
        f"/workers/scripts/{env['SCRIPT_NAME']}/deployments"
    )
    # An idempotent read, so a re-issue is safe; a definitive 4xx still fails
    # fast under -f rather than being retried.
    fetched = subprocess.run(
        [
            "curl",
            "-sf",
            "--retry",
            "5",
            "--retry-connrefused",
            "-H",
            f"Authorization: Bearer {env['CLOUDFLARE_API_TOKEN']}",
            url,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if fetched.returncode != 0:
        sys.stderr.write(fetched.stderr)
        raise SystemExit(fetched.returncode)
    return fetched.stdout


def _fetch_live_sha_body(env: Mapping[str, str], url: str, header: str) -> str:
    # No -f: a non-2xx body (a bot-challenge page, a 429) is captured for the
    # mismatch diagnostic rather than collapsing to an opaque empty string. -sS
    # stays quiet on success but surfaces transport errors.
    command = ["curl", "-sS", "-m", "15"]
    if header:
        command += ["-H", header]
    command.append(url)
    fetched = subprocess.run(command, capture_output=True, text=True, check=False)
    if fetched.returncode != 0:
        sys.stderr.write(fetched.stderr)
        return ""
    return fetched.stdout


def _extract_sha(body: str, sha_filter: str) -> str:
    """Read the SHA out of a live-endpoint body with the caller's jq filter.

    `// empty` collapses a missing or null field to "" so a malformed body never
    matches; the comparison against a non-empty expected SHA does the rest. A jq
    parse error on a non-JSON body is the same empty answer.
    """
    extracted = subprocess.run(
        ["jq", "-r", f"{sha_filter} // empty"],
        input=body,
        capture_output=True,
        text=True,
        check=False,
    )
    if extracted.returncode != 0:
        return ""
    return extracted.stdout.rstrip("\n")


def _verify_live_sha(
    env: Mapping[str, str], script_name: str, expected_sha: str
) -> None:
    url = env.get("VERIFY_DEPLOYMENT_SHA_URL", "")
    if not url:
        raise VerifyFailure(
            "::error::VERIFY_DEPLOYMENT_EXPECTED_SHA is set but VERIFY_DEPLOYMENT_SHA_URL is not"
            " - cannot verify the live SHA",
            to_stderr=True,
        )

    header = env.get("VERIFY_DEPLOYMENT_SHA_HEADER", "")
    sha_filter = env.get("VERIFY_DEPLOYMENT_SHA_JQ") or DEFAULT_SHA_JQ
    attempts = _int_env(env, "VERIFY_DEPLOYMENT_SHA_ATTEMPTS", DEFAULT_SHA_ATTEMPTS)
    interval = _int_env(
        env, "VERIFY_DEPLOYMENT_SHA_INTERVAL_SECONDS", DEFAULT_SHA_INTERVAL_SECONDS
    )

    live_sha = ""
    last_body = ""
    for attempt in range(1, attempts + 1):
        last_body = _fetch_live_sha_body(env, url, header)
        live_sha = _extract_sha(last_body, sha_filter)
        if live_sha == expected_sha:
            print(
                f"Live {script_name} is serving expected SHA {expected_sha}"
                f" (attempt {attempt}/{attempts})"
            )
            return
        print(
            f"Attempt {attempt}/{attempts}: live SHA '{live_sha or '<none>'}'"
            f" != expected '{expected_sha}'"
        )
        if attempt < attempts:
            time.sleep(interval)

    raise VerifyFailure(
        f"::error::{script_name} did not report expected SHA {expected_sha} after"
        f" {attempts} attempts against {url} (last live SHA: '{live_sha or '<none>'}')."
        f" Last response (truncated): {last_body[:BODY_SNIPPET_CHARS]}"
    )


def run(env: Mapping[str, str]) -> None:
    for name in REQUIRED_ENV:
        if not env.get(name):
            raise VerifyFailure(f"::error::{name} is required", to_stderr=True)

    script_name = env["SCRIPT_NAME"]
    latest_deploy = latest_deploy_timestamp(_fetch_deployments(env), script_name)
    deploy_epoch = parse_deploy_epoch(latest_deploy)
    now_epoch = _int_env(env, "VERIFY_DEPLOYMENT_NOW_EPOCH", int(time.time()))
    age_seconds = now_epoch - deploy_epoch

    if age_seconds < 0:
        future_seconds = -age_seconds
        if future_seconds > MAX_FUTURE_SKEW_SECONDS:
            raise VerifyFailure(
                f"::error::Latest deployment timestamp is {future_seconds}s in the future"
            )
        print(
            f"::warning::Latest deployment timestamp is {future_seconds}s ahead of the"
            " runner clock; treating as clock skew"
        )
        age_seconds = 0

    print(f"Latest deployment for {script_name}: {latest_deploy} ({age_seconds}s ago)")

    if age_seconds > MAX_AGE_SECONDS:
        raise VerifyFailure(
            f"::error::Latest deployment is {age_seconds}s old - expected a deployment"
            " within the last 5 minutes"
        )

    # Optional: assert the *live* Worker is serving the expected commit SHA, not
    # just that some deployment is recent (see the module docstring's race).
    # Enabled only when the caller supplies the expected SHA. Edge propagation
    # after a version promotion is near-instant but not instantaneous, so poll a
    # bounded number of times before failing closed.
    expected_sha = env.get("VERIFY_DEPLOYMENT_EXPECTED_SHA", "")
    if expected_sha:
        _verify_live_sha(env, script_name, expected_sha)


def main(_argv: Sequence[str]) -> int:
    try:
        run(os.environ)
    except VerifyFailure as failure:
        print(str(failure), file=sys.stderr if failure.to_stderr else sys.stdout)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
