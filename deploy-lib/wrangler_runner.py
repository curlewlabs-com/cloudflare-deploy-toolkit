"""Runs one wrangler command with bounded retries on classified-transient errors.

The roster of what counts as transient is `wrangler_transient.py`, shared with
the consumer's own Cloudflare reads so an added shape covers every caller at
once. This module owns the loop around it: the attempt budget, the wait between
attempts, what an operator sees while it happens, and the count of retries the
diagnostics bundle reports.

WHAT THE CALLER GETS BACK is the terminal attempt's streams, kept apart. Some
callers parse a JSON document out of stdout and every one of them would be
broken by a retry diagnostic landing in it, so the diagnostics go to this
process's stderr and never into either returned stream.

WHAT AN OPERATOR SEES is every attempt's stderr, not only the last, so a blip
that changes shape between attempts is visible in the CI log. The terminal
attempt's stderr is replayed here too, on every exit path, which is what makes
stdout the caller's to place: a caller capturing a document still gets
wrangler's error text in the log.

The retry count is a plain attribute on this object, which is what lets a
caller that captures a document still contribute to the total the diagnostics
bundle reports.

EVERY WRAPPED OPERATION IS SAFE TO REPEAT. They are declarative updates pinned
to a version id, `UPDATE` statements that settle to the same row, or reads; the
lock and unlock mutations have a ground-truth read-back after them. The one
exception is `versions upload`, which mints a fresh version id per attempt, so a
retry after a lost response leaves an orphan version behind - harmless, and the
succeeding attempt's id is the one that gets promoted.
"""

from __future__ import annotations

import dataclasses
import os
import random
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from typing import Literal

import retry_sleep
import wrangler_transient


def reaches_wrangler(value: str) -> bool:
    """Whether this string can still be handed to wrangler as an argument.

    During recovery, the pipeline spends the serving version id as
    `wrangler rollback <id>` and the Time Travel
    bookmark becomes `--bookmark=<b>`. A value that cannot survive that trip has
    to be refused where it is recorded, while the deploy can still stop cleanly.

    `os.fsencode` is the exact question, and it is narrower than it looks. The
    common case is NOT refused: the streams above are decoded with
    surrogateescape and argv is encoded with surrogateescape too, so a byte that
    is not UTF-8 round-trips to Cloudflare unchanged, and refusing those would
    fail deploys that work. What fails is a lone surrogate the document carries
    as its own JSON escape, which no encoder will take.
    """
    try:
        os.fsencode(value)
    except UnicodeEncodeError:
        return False
    return True


# wrangler renders arbitrary bytes from the API. Decoding with surrogateescape
# and encoding the same way on replay means a byte sequence that is not UTF-8
# reaches the operator's log unchanged instead of raising while the pipeline is
# trying to report a failure.
_STREAM_ERRORS = "surrogateescape"


@dataclasses.dataclass(frozen=True)
class WranglerResult:
    """The terminal attempt's outcome."""

    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def combined(self) -> str:
        return self.stdout + self.stderr


def write_stdout(text: str) -> None:
    """Replay captured wrangler output on stdout, bytes intact."""
    if not text:
        return
    sys.stdout.buffer.write(text.encode("utf-8", _STREAM_ERRORS))
    sys.stdout.flush()


def write_stderr(text: str) -> None:
    """Replay captured wrangler output on stderr, bytes intact."""
    if not text:
        return
    sys.stderr.buffer.write(text.encode("utf-8", _STREAM_ERRORS))
    sys.stderr.flush()


def invoke(argv: Sequence[str]) -> WranglerResult:
    """One wrangler call, with wrangler's interactive banner suppressed.

    CI=true is what stops wrangler printing its "agent skills" banner to STDOUT
    in an agent runtime, which would land inside the JSON documents this
    pipeline parses. It is set on the child rather than exported, so a consumer
    hook keeps the caller's own CI value.

    Public because one call deliberately runs outside the retry loop: the lock
    read in deploy_lock.py, whose own caller retries on any unreadable state,
    which is broader than the transient roster. Layering the narrower classifier
    beneath the broader retry would only reduce reliability.
    """
    try:
        completed = subprocess.run(
            list(argv),
            capture_output=True,
            check=False,
            encoding="utf-8",
            errors=_STREAM_ERRORS,
            env={**os.environ, "CI": "true"},
        )
    except OSError as exc:
        # A wrangler that will not start - not on PATH, not executable - is a
        # failed call rather than a defect in the pipeline, so it takes the same
        # route every other failure takes: the classifier declines to retry it
        # and the step reports it. 127 is what a shell reports for the same
        # thing, which is the status an operator's runbook already knows.
        return WranglerResult(
            returncode=127, stdout="", stderr=f"[FAIL] cannot run {argv[0]}: {exc}\n"
        )
    return WranglerResult(
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


DEFAULT_BACKOFF = (2, 5, 15)
# The view and deploy endpoints can see a new version at different times,
# even after the ordinary retry budget has elapsed.
UPLOAD_VISIBILITY_BACKOFF = (*DEFAULT_BACKOFF, 30, 60)


class WranglerRunner:
    """The retry loop, its schedule, and the run's total retry count."""

    def __init__(
        self,
        *,
        wrangler: Sequence[str],
        backoff_seconds: Sequence[int] | None,
    ) -> None:
        self._wrangler = tuple(wrangler)
        self._backoff = (
            DEFAULT_BACKOFF if backoff_seconds is None else tuple(backoff_seconds)
        )
        self._visibility_backoff = (
            UPLOAD_VISIBILITY_BACKOFF if backoff_seconds is None else self._backoff
        )
        self._retries = 0

    @property
    def retries(self) -> int:
        """Retries performed across the run, for the diagnostics bundle."""
        return self._retries

    def run(self, label: str, args: Sequence[str]) -> WranglerResult:
        return self._run(label, args, just_uploaded=False)

    def run_version(
        self,
        label: str,
        operation: Literal["view", "deploy"],
        version_id: str,
        options: Sequence[str],
        *,
        retry_lookup_for: Callable[[str], bool] | None,
        before_visibility_retry: Callable[[], None] | None = None,
        mark_attempt: Callable[[bool], None] | None = None,
    ) -> WranglerResult:
        """Tie the visibility budget to the same id that wrangler receives."""
        target = f"{version_id}@100%" if operation == "deploy" else version_id
        return self._run(
            label,
            ["versions", operation, target, *options],
            just_uploaded=retry_lookup_for is not None and retry_lookup_for(version_id),
            promotion_id=version_id if operation == "deploy" else None,
            before_visibility_retry=before_visibility_retry,
            mark_attempt=mark_attempt,
        )

    def _run(
        self,
        label: str,
        args: Sequence[str],
        *,
        just_uploaded: bool,
        before_visibility_retry: Callable[[], None] | None = None,
        mark_attempt: Callable[[bool], None] | None = None,
        promotion_id: str | None = None,
    ) -> WranglerResult:
        argv = [*self._wrangler, *args]
        total = len(self._backoff) + 1
        attempt = 1
        ambiguous_attempt = False
        rechecked_promotion = False

        while True:
            if mark_attempt is not None:
                mark_attempt(True)
            result = self._invoke(argv)

            if result.ok:
                write_stderr(result.stderr)
                if attempt > 1:
                    print(
                        f"[deploy-retry] {label}: succeeded on attempt {attempt}/{total}",
                        file=sys.stderr,
                        flush=True,
                    )
                return result

            missing_error = (
                promotion_id is not None
                and wrangler_transient.is_version_not_found(result.stderr)
            )
            target_missing = (
                missing_error
                and promotion_id is not None
                and wrangler_transient.missing_promotion_target(
                    result.stderr, promotion_id
                )
            )
            if target_missing:
                # Wrangler could not resolve the target before submitting this
                # attempt. A lost response from an earlier attempt still counts.
                if mark_attempt is not None and not ambiguous_attempt:
                    mark_attempt(False)
                if not just_uploaded or (before_visibility_retry and ambiguous_attempt):
                    write_stderr(result.stderr)
                    write_stderr(
                        f"[deploy-retry] {label}: target lookup failed, entering recovery\n"
                    )
                    return result

            elif missing_error:
                # A different request can fail after changing traffic. Progress
                # text on stdout cannot turn that verdict into a safe resend.
                write_stderr(result.stderr)
                write_stderr(
                    f"[deploy-retry] {label}: unconfirmed target lookup, entering recovery\n"
                )
                return result
            elif rechecked_promotion:
                # The guarded retry may have applied. Re-sending after another
                # wait could overwrite traffic the serving check never saw.
                write_stderr(result.stderr)
                write_stderr(
                    f"[deploy-retry] {label}: guarded retry failed, entering recovery\n"
                )
                return result

            waiting_for_visibility = just_uploaded and (
                target_missing
                if promotion_id is not None
                else wrangler_transient.is_version_not_found(result.combined)
            )
            if not waiting_for_visibility and not wrangler_transient.is_transient(
                result.combined
            ):
                if attempt > 1:
                    print(
                        f"[deploy-retry] {label}: non-retryable error after "
                        f"{attempt} attempts (exit={result.returncode}), giving up",
                        file=sys.stderr,
                        flush=True,
                    )
                else:
                    print(
                        f"[deploy-retry] {label}: non-retryable error "
                        f"(exit={result.returncode}), not retrying",
                        file=sys.stderr,
                        flush=True,
                    )
                write_stderr(result.stderr)
                return result

            schedule = (
                self._visibility_backoff if waiting_for_visibility else self._backoff
            )
            total = len(schedule) + 1
            if attempt >= total:
                print(
                    f"[deploy-retry] {label}: exhausted {attempt} attempts, "
                    f"giving up (exit={result.returncode})",
                    file=sys.stderr,
                    flush=True,
                )
                write_stderr(result.stderr)
                return result

            wait = retry_sleep.sleep_seconds(
                schedule[attempt - 1], retry_sleep.draw_jitter(random.SystemRandom())
            )
            ambiguous_attempt |= not waiting_for_visibility
            print(
                f"[deploy-retry] {label}: attempt {attempt}/{total} failed with "
                f"transient error (exit={result.returncode}), retrying in {wait}s",
                file=sys.stderr,
                flush=True,
            )
            # Each failing attempt's stderr, not only the last, so an operator
            # reading the CI log sees whether the blip changed shape.
            write_stderr(result.stderr)
            time.sleep(wait)
            if waiting_for_visibility and before_visibility_retry is not None:
                before_visibility_retry()
                rechecked_promotion = True
            self._retries += 1
            attempt += 1

    def _invoke(self, argv: Sequence[str]) -> WranglerResult:
        return invoke(argv)
