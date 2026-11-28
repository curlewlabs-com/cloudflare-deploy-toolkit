#!/bin/sh
# Minimal example consumer wrapper for the deploy pipeline.
#
# This script exists to drive the pipeline end-to-end from the toolkit's own
# self-tests, AND to serve as the smallest possible working example for someone
# writing their first wrapper. It is NOT meant to be deployed to a real
# Cloudflare account - the config values are placeholders and the hooks are
# no-ops.
#
# Real consumer wrappers look almost identical to this, just with:
#   - real DEPLOY_WRANGLER_ENV_* values
#   - hook commands that actually run schema checks and migrations against your
#     D1 database
#   - a path that points at your vendored copy of the toolkit
#
# Exported config and executable hook commands are worth naming here because they follow from
# the pipeline being a program the wrapper starts rather than a library it
# sources, and a wrapper that gets either wrong fails at startup:
#
#   - config is EXPORTED, because a subprocess reads its environment and cannot
#     see a shell variable the wrapper merely assigned;
#   - hooks are COMMANDS, run with the environment appended and, for preflight, the captured version ID.
#
# See deploy-lib/README.md for the full API.

set -eu

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
HARNESS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
TOOLKIT_DIR="$(cd "$HARNESS_DIR/.." && pwd)"

# === required config (read by the pipeline from its environment) ===
export DEPLOY_WRANGLER_ENV_STAGING="staging"
export DEPLOY_WRANGLER_ENV_PRODUCTION="production"

# Hook commands receive the environment; preflight also receives the version ID.
# Defaulted rather than assigned so a test driver can substitute a builtin -
# `true` for a no-op, `false` to inject a failure - without editing the wrapper.
export PREFLIGHT_CMD="${PREFLIGHT_CMD:-$SCRIPT_DIR/example-hook.sh preflight}"
export MIGRATE_CMD="${MIGRATE_CMD:-$SCRIPT_DIR/example-hook.sh migrate}"
export POSTFLIGHT_CMD="${POSTFLIGHT_CMD:-$SCRIPT_DIR/example-hook.sh postflight}"

# === run ===
# Named version first, then the bare name: `python3` can resolve below the floor
# the toolkit's Python is written against - Apple's /usr/bin/python3 is the common
# case - and a vendored copy gets the same ladder rather
# than any one repo's virtualenv. A consumer wrapper owns this choice because
# the pipeline is the process it starts; a consumer's own scripts get the same
# ladder from wrangler-retry.sh, which resolves it when sourced.
if command -v python3.14 >/dev/null 2>&1; then
    PYTHON=python3.14
elif command -v python3 >/dev/null 2>&1; then
    PYTHON=python3
else
    echo "[FAIL] example wrapper: no python3 on PATH" >&2
    exit 1
fi

exec "$PYTHON" "$TOOLKIT_DIR/deploy-lib/safe_deploy_pipeline.py" "$@"
