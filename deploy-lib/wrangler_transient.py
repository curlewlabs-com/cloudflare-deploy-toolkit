#!/usr/bin/env python3
"""The transient-error roster for wrangler output, and the CLI that classifies
against it.

This module is the single authority for which wrangler failures are worth
another attempt, and the Python pipeline and shell retry helper route to it. The
pipeline imports it and
calls `is_transient` (`wrangler_runner.py`). A consumer's own shell scripts
source `wrangler-retry.sh` and reach the same rows through
`deploy_output_is_transient`, which spawns the CLI below. One roster either way,
so adding an error shape covers every caller at once instead of the subset whose
copy someone remembered to edit.

Scope is idempotent READS. A write needs its own idempotency analysis before it
may retry, and the deploy path's writes already carry one: the schema and stats
imports retry under their own bounded loops, which re-read live state and
classify where the import landed before re-attempting, and the rollback floor's
advance is monotonic by construction with a read-back after it.

Conservative allowlist: only error shapes positively identified as transient,
and only where retry is safe because the wrapped operation is idempotent or has
a ground-truth read-back after it. Missing a pattern means failing without
retry, which is the same as no-retry behavior; the risky direction would be
wrongly classifying an auth or validation error as retryable, which an
allowlist avoids.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Sequence

# One status, an API-envelope render and an edge-error-page render. What is classified here is wrangler's rendered text,
# not an HTTP status read off a response, and wrangler renders the same
# Cloudflare 5xx according to who produced the body:
#
#   the API, as a JSON envelope -> wrangler parses it and prints `HTTP <status>`
#   the edge, as an error page  -> wrangler cannot parse it, prints "Received a
#                                  malformed response from the API", and dumps
#                                  the raw exchange: `<METHOD> <path> -> <status>
#                                  <reason>` plus the proxy's own body text
#
# A roster carrying only the envelope render therefore misses every 5xx that
# never reached the API - and an edge that cannot reach its backend is exactly
# the blip a retry exists to ride out. So the edge render is matched by the
# status its dump carries, never by its header line alone: what that header
# says is that the BODY did not parse, and the edge writes one over a 403 or a
# validation page as readily as over a 503. Within that render the status is
# what separates a blip from an answer. A body that names a failed connection
# is classified by the transport strings below, before any status is consulted.
TRANSPORT_SUBSTRINGS: tuple[str, ...] = (
    "fetch failed",
    "EAI_AGAIN",
    "ECONNRESET",
    "ETIMEDOUT",
    "ENETUNREACH",
    # Same class as the errno strings above, one hop further out: the proxy in
    # front of the API emits this only when it could not reach its upstream, so
    # it names a connection that failed rather than a response that came back.
    # There is no verdict it can accompany - a proxy that reached the upstream
    # relays whatever the upstream said instead.
    "upstream connect error",
)

# Rendered by the API as text rather than as a status.
API_SUBSTRINGS: tuple[str, ...] = (
    "code: 7429",
    "storage operation exceeded timeout",
    "Too Many Requests",
)

# Statuses blessed as transient. 429 plus the 5xx family wrangler surfaces:
# server-side and edge-side failures that a later attempt can find healthy.
TRANSIENT_STATUSES: tuple[int, ...] = (429, 500, 502, 503, 504, 520, 521, 522, 523, 524)

# Envelope render: the status as wrangler prints it once it has parsed the JSON
# body, matched as a plain substring. The edge arm below carries a right
# boundary because its dump writes a duration or a longer number into the
# position just after the status (`-> 520ms`, `-> 5200000`); no instance of the
# envelope render doing that has been seen, so this arm claims no boundary that
# an instance has not asked for.
ENVELOPE_SUBSTRINGS: tuple[str, ...] = tuple(f"HTTP {s}" for s in TRANSIENT_STATUSES)

# The edge render's statuses. 429 is absent because it never needs this arm -
# its reason phrase ("Too Many Requests") is already an entry in its own right.
EDGE_RENDER_STATUSES: tuple[int, ...] = tuple(s for s in TRANSIENT_STATUSES if s != 429)

# The right boundary is the ASCII whitespace set, written out rather than as
# `\s`, so the verdict does not move with the host's locale. `\s` would also
# match Unicode separators, and CI hosts span C-locale Linux runners and
# UTF-8 macOS boxes; a retry decision that depends on which one ran it is a
# decision nobody can reproduce. The set is the narrow reading, which is the
# direction the allowlist errs in everywhere else: the render being matched
# separates a status from its reason phrase with an ASCII space, so nothing in
# it distinguishes a literal status from a longer status-like token.
_ASCII_SPACE_CLASS = "[ \t\n\r\f\v]"

# Edge render, matched on the `-> ` the dump puts before the status and never
# on the reason phrase after it. The reasons, and the second is why this is not
# a list of literal phrases. The arrow is the part of the render we have an
# instance of, whereas Cloudflare's own 52x family carries reason phrases this
# repo has never seen printed - and a guessed phrase does not fail loudly, it
# just never fires. Keying on the status instead costs nothing, since the phrase
# adds no information the status lacks.
#
# Each arm has leading and trailing bounds. A status line ends its status with
# whitespace or with the output itself, so the lookahead is the whole right
# boundary. Its leading and trailing bounds must stay narrow: a digit continuing the
# number means this was never a 520 (`-> 5200000`), and so does a letter, which
# is how a duration renders (`-> 520ms`). Anything looser widens the allowlist
# toward retrying output that names no status at all.
_EDGE_RE = re.compile(
    "|".join(rf" -> {s}(?={_ASCII_SPACE_CLASS}|\Z)" for s in EDGE_RENDER_STATUSES)
)


def is_transient(combined_output: str) -> bool:
    """True when this wrangler output names a failure worth another attempt."""
    for needle in (*TRANSPORT_SUBSTRINGS, *API_SUBSTRINGS, *ENVELOPE_SUBSTRINGS):
        if needle in combined_output:
            return True
    return _EDGE_RE.search(combined_output) is not None


# The version endpoint's not-found. A staging deploy's runtime check got it
# reading back, a second later, the id its own upload had just returned, and a
# re-run passed; for such an id the read is safe to repeat within a bound. It
# is NOT in the roster above, because for any other id it is an answer - the
# version does not exist, and asking again changes nothing. Only a caller that
# knows its id came from an upload it just made may retry this response.
# Bounded on the right so a longer code that begins with these digits is not
# read as this one, the boundary the status arms above keep for the same reason.
_VERSION_NOT_FOUND_RE = re.compile(r"code: 100146(?![0-9])")


def is_version_not_found(combined_output: str) -> bool:
    """True when this wrangler output is the version endpoint's not-found."""
    return _VERSION_NOT_FOUND_RE.search(combined_output) is not None


def main(argv: Sequence[str]) -> int:
    """Read wrangler's combined output on stdin; 0 = transient, 1 = not.

    stdin rather than argv because the combined stdout and stderr of a failed
    wrangler call has no bounded size and argv does. The whole stream is read
    before any decision so that the writer on the other end of the pipe never
    faults on a closed reader.
    """
    if argv:
        print(
            "usage: wrangler_transient.py  (reads wrangler output on stdin)",
            file=sys.stderr,
        )
        return 2

    # surrogateescape rather than a lossy decode: wrangler output is arbitrary
    # bytes, and mapping an undecodable byte to U+FFFD could manufacture a
    # character the roster matches on. An escaped byte matches no pattern here,
    # so an undecodable stream classifies on the parts that did decode.
    raw = sys.stdin.buffer.read()
    return 0 if is_transient(raw.decode("utf-8", errors="surrogateescape")) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
