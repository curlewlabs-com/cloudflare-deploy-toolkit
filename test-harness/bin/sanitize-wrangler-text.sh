#!/bin/sh
# Scrubs wrangler text output for fixture capture.
#
# Reads raw wrangler stdout/stderr from stdin and writes a deterministic
# sanitized version to stdout. Strips PII (account IDs, emails, real version
# UUIDs), normalizes wrangler's version banner and upload-size strings, and
# (for upload modes) replaces the dry-run trailer with a synthetic
# `Worker Version ID:` line so the captured fixture matches the format of a
# real (non-dry-run) upload - `versions upload --dry-run` elides the
# version-id line entirely, but every consumer of this harness greps for it,
# so the synthetic line bridges the gap.
#
# Used by:
#   - Consumer capture scripts: pipe real wrangler output through this when
#     re-capturing fixtures after a wrangler version bump.
#   - test-sanitize-wrangler-text.bats: feeds synthetic input to assert each
#     scrub pattern works in isolation, without needing a real wrangler binary.
#
# Why a separate script (not a shell function): the unit test exercises this
# as a black-box stdin->stdout transform. Inlining it as a function in a
# capture script would require sourcing tricks to test, which is fragile.
#
# Usage: sanitize-wrangler-text.sh <mode> [--scrub-binding NAME ...]
# Modes:
#   upload-maint  - versions upload --var MAINTENANCE_MODE:true output
#   upload-final  - versions upload (no maintenance var) output
#   triggers      - triggers deploy output (no Worker Version ID injection)
#
# --scrub-binding NAME: replace `env.NAME ("...")` value with `("REDACTED")`.
# Repeatable. Use it for binding names whose values are PII or vary across
# captures (e.g. a Firebase test UID hardcoded in wrangler.toml). The
# default is empty - no consumer-specific scrubs.

set -eu

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
HARNESS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
# shellcheck source=SCRIPTDIR/../lib/test-slots.sh
. "$HARNESS_DIR/lib/test-slots.sh"

MODE=""
SCRUB_BINDINGS=""
while [ $# -gt 0 ]; do
    case "$1" in
    upload-maint | upload-final | triggers)
        MODE="$1"
        ;;
    --scrub-binding)
        shift
        if [ -z "${1:-}" ]; then
            echo "sanitize-wrangler-text.sh: --scrub-binding requires an argument" >&2
            exit 2
        fi
        SCRUB_BINDINGS="$SCRUB_BINDINGS $1"
        ;;
    -h | --help | "")
        cat >&2 <<EOF
Usage: $0 <mode> [--scrub-binding NAME ...]

Reads wrangler text output from stdin, writes sanitized fixture to stdout.

Modes:
  upload-maint  versions upload --var MAINTENANCE_MODE:true output
  upload-final  versions upload (no maintenance var) output
  triggers      triggers deploy output (no Worker Version ID injection)

Options:
  --scrub-binding NAME    Replace env.NAME ("...") value with ("REDACTED").
                          Repeatable. Use for binding names whose values
                          are consumer-specific PII.

The fixed UUIDs injected for upload-maint (11111111-...) and upload-final
(22222222-...) are defined in lib/test-slots.sh and shared with the mock
and consumer test drivers.
EOF
        exit 2
        ;;
    *)
        echo "sanitize-wrangler-text.sh: unknown argument: $1" >&2
        exit 2
        ;;
    esac
    shift
done

case "$MODE" in
upload-maint) INJECT_VERSION_ID="$WRANGLER_HARNESS_MAINT_UUID" ;;
upload-final) INJECT_VERSION_ID="$WRANGLER_HARNESS_FINAL_UUID" ;;
triggers) INJECT_VERSION_ID="" ;;
*)
    echo "sanitize-wrangler-text.sh: mode is required" >&2
    exit 2
    ;;
esac

# Build the consumer-binding scrub script. These run after the ANSI strip,
# because wrangler colors a binding's name and value separately and the codes
# between them would otherwise stop the pattern matching, and BEFORE the
# generic scrubs, so that a binding value matching a generic pattern (e.g. a
# token that looks like a UUID) gets the consumer-specific REDACTED placeholder
# rather than the generic zero placeholder. Wrangler prints a binding as
# `env.NAME (value)`; older releases quoted the value, `env.NAME ("value")`.
# A value can hold a parenthesis or an escaped quote, so no closing delimiter
# marks where it ends: everything from the opening parenthesis to the end of
# the line is replaced, which drops the binding-type column after it too.
CONSUMER_SCRUBS=""
for binding in $SCRUB_BINDINGS; do
    CONSUMER_SCRUBS="${CONSUMER_SCRUBS}
  s/(env\\.${binding}) \\(\".*/\\1 (\"REDACTED\")/
  s/(env\\.${binding}) \\([^\"].*/\\1 (REDACTED)/"
done

# Literal ESC byte for ANSI escape code matching. Wrangler emits color codes
# even when stdout is redirected (verified with od -c against captured output),
# so we have to strip them at the byte level rather than relying on a TTY check.
ESC=$(printf '\033')

# Apply scrubbers in order. The order matters:
#   0. ANSI strip - every regex after it assumes plain text.
#   1. Consumer-specific binding values (before the generic catch-all scrubs
#      below, so they take precedence).
#   2. Wrangler version banner normalization - eats the "(update available ...)"
#      parenthetical so the fixture doesn't churn on every wrangler bump.
#   3. Total Upload size - varies on every code change to the Worker.
#   4. ISO 8601 timestamps - wrangler may emit them in upload metadata; pin
#      to a fixed value so re-capture is deterministic.
#   5. Email addresses - author_email may appear in some outputs.
#   6. Generic 36-char dash UUIDs - defense in depth for any wrangler-emitted
#      UUID we didn't anticipate. Replaces with a slot-zero placeholder.
#   7. Generic 32-char hex (Cloudflare account IDs) - same defense-in-depth.
sed -E "s/${ESC}\\[[0-9;]*m//g
${CONSUMER_SCRUBS}
  s/wrangler [0-9]+\\.[0-9]+\\.[0-9]+( \\(update available [0-9]+\\.[0-9]+\\.[0-9]+\\))?/wrangler 0.0.0/
  s/Total Upload: [0-9.]+ KiB \\/ gzip: [0-9.]+ KiB/Total Upload: 0.00 KiB \\/ gzip: 0.00 KiB/
  s/[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\\.[0-9]+)?Z/${WRANGLER_HARNESS_TEST_TIMESTAMP}/g
  s/[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\\.[A-Za-z]{2,}/${WRANGLER_HARNESS_TEST_EMAIL}/g
  s/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/${WRANGLER_HARNESS_LATEST_UUID}/g
  s/(^|[^0-9a-f])[0-9a-f]{32}([^0-9a-f]|\$)/\100000000000000000000000000000000\2/g
" | if [ -n "$INJECT_VERSION_ID" ]; then
    sed -E "s|--dry-run: exiting now\\.|Worker Version ID: $INJECT_VERSION_ID|"
else
    cat
fi
