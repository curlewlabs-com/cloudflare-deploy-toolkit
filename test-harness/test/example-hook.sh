#!/bin/sh
# Minimal example consumer hook for the Python deploy pipeline.
#
# The wrapper configures the label as a leading argument. The pipeline supplies
# the target environment and, for preflight, the immutable serving version ID.
# Consumer preflights use that ID to check compatibility before remote changes.
#
# Usage: example-hook.sh <label> <env> [serving-version-id]
set -eu

# A hook that never finishes, and that starts a child the way a real hook
# starts the program doing its work. It records its own process id and the
# child's, so a test can tell whether stopping the hook stopped what it started.
# The same, with a child that ignores SIGTERM, as a program mid-write may. An
# ignored signal stays ignored across exec, so the sleep below cannot be asked
# to stop, only made to.
if [ "$1" = "hang-ignoring-sigterm" ]; then
    sh -c 'trap "" TERM; : >"$EXAMPLE_HOOK_PID_FILE.child-ready"; exec sleep 3600' >/dev/null 2>&1 &
    child_pid=$!
    # Publishing sooner lets the deadline signal the child before it ignores
    # SIGTERM, falsely passing a test meant to require SIGKILL escalation.
    until [ -f "$EXAMPLE_HOOK_PID_FILE.child-ready" ]; do
        sleep 0.01
    done
    printf '%s %s\n' "$$" "$child_pid" >"$EXAMPLE_HOOK_PID_FILE"
    wait
fi

if [ "$1" = "hang" ]; then
    # Off the hook's own output: a test harness that captures that output waits
    # for every holder of it, so a child left running would stall the harness
    # until it finished and then look as though it had been stopped.
    sleep 3600 >/dev/null 2>&1 &
    printf '%s %s\n' "$$" "$!" >"$EXAMPLE_HOOK_PID_FILE"
    wait
fi

echo "[example-$1] env=$2 (no-op)"
