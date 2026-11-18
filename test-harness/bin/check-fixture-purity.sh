#!/bin/sh
# Scans a directory of wrangler fixtures for PII / non-deterministic patterns.
#
# This is the boundary test for any consumer's capture script: even if the
# sanitizer is correct, a future edit could bypass it (someone hand-edits a
# fixture, or the capture script gets a new subcommand whose output isn't
# routed through the sanitizer). This script scans the *committed* fixtures
# and fails if anything looks like it leaked through.
#
# Each forbidden pattern gets its own assertion + clear error message so a
# regression names exactly which file and which pattern. The point is to
# turn a quiet leak into a loud fail-the-PR test.
#
# Generic patterns (always checked):
#   - 36-char dash UUIDs that aren't in the test slot set
#   - 32-char hex (Cloudflare account ID shape)
#   - email addresses on any domain other than example.com
#   - ISO 8601 timestamps that aren't the fixed test timestamp
#   - "(update available" - wrangler version banner not normalized
#   - real wrangler version strings (anything other than 0.0.0)
#   - ANSI escape sequences (CSI start byte)
#
# Consumer-specific forbidden literals are loaded from the optional second
# argument: a file with one literal substring per line. Lines starting with
# `#` and empty lines are ignored. Project-specific scrubs (Firebase test
# UIDs, internal usernames, real account IDs, custom commit-message
# prefixes, etc.) go in that file.
#
# Usage: check-fixture-purity.sh <fixture-dir> [<forbidden-literals-file>]

set -eu

# ggrep, and a hard stop without it. One pattern below is word-bounded with
# `\b`, which POSIX ERE does not define: BSD grep reads it as a literal, the
# scan then matches nothing, and THIS SCANNER REPORTS SUCCESS - the quiet leak
# it exists to turn loud. So the scan requires the g-name and refuses to run
# without it rather than falling back to whatever grep is on PATH.
if ! command -v ggrep >/dev/null 2>&1; then
    echo "ERROR: check-fixture-purity.sh needs ggrep (GNU grep)." >&2
    echo "  macOS: brew install grep; Linux: link ggrep to the system GNU grep" >&2
    echo "  On Debian / Ubuntu: sudo ln -sf /bin/grep /usr/local/bin/ggrep" >&2
    echo "  On other Linux: put GNU grep on PATH as ggrep" >&2
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
HARNESS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
# shellcheck source=SCRIPTDIR/../lib/test-slots.sh
. "$HARNESS_DIR/lib/test-slots.sh"

FIXTURE_DIR="${1:-}"
LITERALS_FILE="${2:-}"

if [ -z "$FIXTURE_DIR" ]; then
    cat >&2 <<EOF
Usage: $0 <fixture-dir> [<forbidden-literals-file>]

  fixture-dir              Directory containing the consumer's captured
                           wrangler fixtures (e.g. test/fixtures/wrangler/).
  forbidden-literals-file  Optional. One literal substring per line; the
                           scanner fails if any appears in any fixture.
                           Lines starting with # and blank lines are skipped.
EOF
    exit 2
fi

if [ ! -d "$FIXTURE_DIR" ]; then
    echo "[FAIL] check-fixture-purity: fixture dir not found at $FIXTURE_DIR" >&2
    exit 1
fi

PASS=0
FAIL=0

pass() {
    echo "  [OK] $1"
    PASS=$((PASS + 1))
}
fail() {
    echo "  [FAIL] $1" >&2
    FAIL=$((FAIL + 1))
}

RUN_COMPLETE=0
WORK=$(mktemp -d)
# A scan that stopped short must fail, never pass as a clean one: after a
# `set -u` abort, macOS /bin/sh enters this trap with a stale `$?` of 0.
# shellcheck disable=SC2154  # the trap body assigns _trap_rc before it reads it
trap '_trap_rc=$?; [ "$RUN_COMPLETE" = 1 ] || [ "$_trap_rc" -ne 0 ] || _trap_rc=1; rm -rf "$WORK"; exit "$_trap_rc"' EXIT HUP INT TERM

# The fixture list, one path per line, read back line by line so a path holding
# a space or a tab stays one path. A newline inside a name cannot survive that,
# so such a name is refused rather than scanned in pieces. A find that fails
# fails the scan: an unread directory is not a clean one.
NEWLINE_NAMES="$WORK/newline-names"
if ! find "$FIXTURE_DIR" -type f -name '*
*' >"$NEWLINE_NAMES"; then
    echo "[FAIL] check-fixture-purity: could not list $FIXTURE_DIR" >&2
    exit 1
fi
if [ -s "$NEWLINE_NAMES" ]; then
    echo "[FAIL] check-fixture-purity: a fixture name holds a newline; rename it:" >&2
    cat "$NEWLINE_NAMES" >&2
    exit 1
fi
FIXTURES="$WORK/fixtures"
if ! find "$FIXTURE_DIR" -type f \( -name '*.txt' -o -name '*.json' \) >"$WORK/found"; then
    echo "[FAIL] check-fixture-purity: could not list $FIXTURE_DIR" >&2
    exit 1
fi
sort "$WORK/found" >"$FIXTURES"

if [ ! -s "$FIXTURES" ]; then
    echo "[FAIL] check-fixture-purity: no fixture files found in $FIXTURE_DIR" >&2
    exit 1
fi

echo "> scanning $(wc -l <"$FIXTURES" | tr -d ' ') fixture files in $FIXTURE_DIR"
echo ""

# A grep's exit status is the verdict here, and only two of its values are
# answers: 0 matched, 1 did not. Anything else (an unreadable file, a bad
# pattern) is a scan that did not happen, and it fails the run rather than
# passing as a clean file.
unreadable() {
    fail "could not scan $1 (grep exit $2)"
}

# scan_with <grep flags> <pattern> <description>
# Fails with each matching file and line when any fixture matches.
scan_with() {
    flags="$1"
    pattern="$2"
    description="$3"
    hits="$WORK/hits"
    : >"$hits"
    while IFS= read -r f; do
        rc=0
        grep "$flags" -n -- "$pattern" "$f" >"$WORK/lines" 2>/dev/null || rc=$?
        case "$rc" in
        0)
            {
                echo "      in $(basename "$f"):"
                sed 's/^/        /' "$WORK/lines"
            } >>"$hits"
            ;;
        1) ;;
        *) unreadable "$f" "$rc" ;;
        esac
    done <"$FIXTURES"
    if [ -s "$hits" ]; then
        fail "$description"
        cat "$hits" >&2
    else
        pass "$description"
    fi
}

# scan_pattern <pattern> <description>: ERE match.
scan_pattern() {
    scan_with -E "$1" "$2"
}

# scan_literal <literal> <description>: plain-string match, for consumer
# literals where regex metacharacters might appear without intent.
scan_literal() {
    scan_with -F "$1" "$2"
}

# scan_pattern_excluding <pattern> <description> <allowlist regex>
# Same as scan_pattern but filters out matches that also match the allowlist.
# Used for "any UUID OTHER than the test slot UUIDs" and similar.
scan_pattern_excluding() {
    pattern="$1"
    description="$2"
    allowlist="$3"
    hits="$WORK/hits"
    : >"$hits"
    while IFS= read -r f; do
        # ggrep, guarded at the top: `\b` below needs GNU semantics.
        rc=0
        ggrep -E -o -- "$pattern" "$f" >"$WORK/found-matches" 2>/dev/null || rc=$?
        case "$rc" in
        0) ;;
        1) continue ;;
        *)
            unreadable "$f" "$rc"
            continue
            ;;
        esac
        rc=0
        ggrep -E -v -- "$allowlist" "$WORK/found-matches" >"$WORK/kept" || rc=$?
        case "$rc" in
        0)
            {
                echo "  in $(basename "$f"):"
                sed 's/^/    /' "$WORK/kept"
            } | sed 's/^/      /' >>"$hits"
            ;;
        1) ;;
        *) unreadable "$f" "$rc" ;;
        esac
    done <"$FIXTURES"
    if [ -s "$hits" ]; then
        fail "$description"
        cat "$hits" >&2
    else
        pass "$description"
    fi
}

# ----------------------------------------------------------------------
# Generic pattern checks
# ----------------------------------------------------------------------

# UUIDs other than the test slot set. Test slots are zero-fill, all-1, all-2,
# all-9, plus the structural placeholders aaaaaaaaaaaa / bbbbbbbbbbbb.
scan_pattern_excluding \
    "[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}" \
    "no real 36-char dash UUIDs (only test slot UUIDs allowed)" \
    "^(00000000-0000-0000-0000-000000000000|11111111-1111-1111-1111-111111111111|22222222-2222-2222-2222-222222222222|99999999-9999-9999-9999-999999999999|00000000-0000-0000-0000-aaaaaaaaaaaa|00000000-0000-0000-0000-bbbbbbbbbbbb)$"

# 32-char hex (Cloudflare account ID shape). Word-bounded so we don't false-
# positive on substrings of dash-separated bookmark hex.
scan_pattern_excluding \
    "\\b[0-9a-f]{32}\\b" \
    "no real 32-char hex strings (account ID shape; only zero placeholder allowed)" \
    "^(00000000000000000000000000000000|aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa)$"

scan_pattern_excluding \
    "[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\\.[A-Za-z]{2,}" \
    "no email addresses outside example.com" \
    "@example\\.com$"

scan_pattern_excluding \
    "[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\\.[0-9]+)?Z" \
    "no ISO 8601 timestamps other than ${WRANGLER_HARNESS_TEST_TIMESTAMP}" \
    "^${WRANGLER_HARNESS_TEST_TIMESTAMP}$"

scan_pattern \
    "\\(update available" \
    "no '(update available' wrangler version warnings"

scan_pattern_excluding \
    "wrangler [0-9]+\\.[0-9]+\\.[0-9]+" \
    "no real wrangler version strings (only 'wrangler 0.0.0' allowed)" \
    "^wrangler 0\\.0\\.0$"

# ANSI escape sequences. Uses the literal ESC byte (0x1b) so the test detects
# what wrangler actually emits, not the printable approximation.
ESC=$(printf '\033')
scan_pattern \
    "${ESC}\\[" \
    "no ANSI escape sequences (CSI start byte)"

# ----------------------------------------------------------------------
# Consumer-supplied literal checks
# ----------------------------------------------------------------------
if [ -n "$LITERALS_FILE" ]; then
    if [ ! -f "$LITERALS_FILE" ]; then
        echo "[FAIL] forbidden-literals file not found: $LITERALS_FILE" >&2
        exit 1
    fi
    echo ""
    echo "> scanning for consumer-specific forbidden literals from $LITERALS_FILE"
    while IFS= read -r line || [ -n "$line" ]; do
        case "$line" in
        "" | "#"*) continue ;;
        esac
        scan_literal "$line" "no '$line' literal"
    done <"$LITERALS_FILE"
fi

echo ""
if [ "$FAIL" -gt 0 ]; then
    echo "[FAIL] check-fixture-purity: $FAIL assertions failed (out of $((PASS + FAIL)))"
    echo ""
    echo "  These leaks indicate the sanitizer in the consumer's capture script missed"
    echo "  something. Either fix the sanitizer and re-run capture, or hand-scrub the"
    echo "  fixture and document why the sanitizer doesn't catch it."
    exit 1
fi
echo "[OK] check-fixture-purity: all $PASS assertions passed"
RUN_COMPLETE=1
