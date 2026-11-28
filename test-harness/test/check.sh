#!/bin/sh
# Self-tests for the toolkit. Run from CI to validate that every piece
# of the toolkit (deploy-lib, mock-wrangler, capture helpers, sanitizer,
# example wrapper) wires together correctly.
#
# What runs:
#   - bats on every test/*.bats file; each suite's header says what it covers
#   - the Python unit suites beside this file, which are what pin the gates in
#     bin/ - the provenance gate's HEAD and SHA modes against real throwaway repos, the
#     CI-execution gate's allow/deny matrix, and the deployment verifier's
#     timestamp, skew, and live-SHA behavior
#
# Bats-core and Python power the toolkit self-tests. Install via
# `brew install bats-core jq python@3.14 grep` on Mac,
# `sudo apt install -y bats jq python3.14` plus
# `sudo ln -sf /bin/grep /usr/local/bin/ggrep` on Debian/Ubuntu.
# The macOS-only `grep` formula and that symlink supply `ggrep`, which
# check-fixture-purity.sh requires and refuses to run without.

set -eu

TEST_DIR="$(cd "$(dirname "$0")" && pwd)"
HARNESS_DIR="$(cd "$TEST_DIR/.." && pwd)"
TOOLKIT_DIR="$(cd "$HARNESS_DIR/.." && pwd)"
RELOCATE_TMPDIR=""

SUITE_COMPLETE=0
cleanup() {
    if [ -n "$RELOCATE_TMPDIR" ]; then
        rm -rf "$RELOCATE_TMPDIR" || echo "WARN: cleanup failed (test result unaffected): $RELOCATE_TMPDIR" >&2
    fi
    [ "$SUITE_COMPLETE" = 1 ] || exit 1
}
trap cleanup EXIT HUP INT TERM

if ! command -v bats >/dev/null 2>&1; then
    echo "[FAIL] bats-core not on PATH" >&2
    echo "  Mac:                brew install bats-core jq python@3.14 grep" >&2
    echo "  Debian / Ubuntu:    sudo apt install -y bats jq python3.14" >&2
    echo "                      plus ggrep -> the system GNU grep on Linux" >&2
    echo "  Other Linux:        install bats, jq, and python3 via your package manager" >&2
    exit 1
fi

echo "> bats unit tests"
# The suites assert with bare `[[ ]]` under errexit. A bash that runs on past a
# failing `[[ ]]` (macOS /bin/bash 3.2 does) fails a test only on its last
# assertion, so every suite would pass whatever the code under test does.
if bash -c 'set -e; [[ a == b ]]; exit 0' >/dev/null 2>&1; then
    echo "[FAIL] the bash on PATH ($(command -v bash)) does not stop on a failing [[ ]] under set -e" >&2
    echo "  Put a current bash first on PATH (macOS: brew install bash)" >&2
    exit 1
fi
bats "$TEST_DIR"/*.bats

echo ""
echo "> python unit tests"
# Named version first, then the bare name: `python3` can resolve below the floor
# the gates are written against (Apple's /usr/bin/python3 is the case that
# exists), and a vendored consumer gets the same ladder rather than any one
# repo's virtualenv, which the toolkit must not depend on.
if command -v python3.14 >/dev/null 2>&1; then
    PYTHON=python3.14
elif command -v python3 >/dev/null 2>&1; then
    PYTHON=python3
else
    echo "[FAIL] no python3 on PATH" >&2
    echo "  Mac:                brew install bats-core jq python@3.14 grep" >&2
    echo "  Debian / Ubuntu:    sudo apt install -y bats jq python3.14" >&2
    echo "                      plus ggrep -> the system GNU grep on Linux" >&2
    echo "  Other Linux:        install bats, jq, and python3 via your package manager" >&2
    exit 1
fi
for suite in "$TEST_DIR"/test_*.py; do
    echo "  === $(basename "$suite") ==="
    "$PYTHON" "$suite"
done

if [ "${DEPLOY_TOOLKIT_SKIP_RELOCATION_SMOKE:-0}" != "1" ]; then
    echo ""
    echo "> relocation smoke"
    # Exercise the installed layout to catch paths tied to this checkout.
    RELOCATE_TMPDIR=$(mktemp -d)
    mkdir -p "$RELOCATE_TMPDIR/vendor"
    cp -R "$TOOLKIT_DIR" "$RELOCATE_TMPDIR/vendor/cloudflare-deploy-toolkit"
    DEPLOY_TOOLKIT_SKIP_RELOCATION_SMOKE=1 \
        "$RELOCATE_TMPDIR/vendor/cloudflare-deploy-toolkit/test-harness/test/check.sh"
    echo "  [OK] harness self-tests pass from a vendored toolkit path"
fi

echo ""
echo "[OK] harness self-tests pass"
SUITE_COMPLETE=1
