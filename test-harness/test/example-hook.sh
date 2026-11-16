#!/bin/sh
# Minimal example consumer hook for the Python deploy pipeline.
#
# The pipeline runs each hook as a COMMAND with the environment name appended
# as the last argument, so a hook command may carry its own leading arguments -
# which is what lets one script stand in for preflight, migration, and post-flight hooks here. A real consumer's
# hooks run schema lints, migrations, and post-flight checks; these no-op echoes
# are enough for the smoke test, which substitutes MIGRATE_CMD and
# POSTFLIGHT_CMD anyway.
#
# Usage: example-hook.sh <label> <env>
set -eu

# A hook's effect on a deploy lock, for the relocation tests. The body depends
# on its copy hook leaving the target filled and LOCKED, and has to notice a
# hook that left a lock any other way. EXAMPLE_HOOK_SETS is a whitespace-separated
# list of <label>=<lock-state file>=<state>; an entry applies when this hook runs
# under that label.
# shellcheck disable=SC2086 # split on purpose: the value is a list
for _entry in ${EXAMPLE_HOOK_SETS:-}; do
    [ "${_entry%%=*}" = "$1" ] || continue
    _rest="${_entry#*=}"
    printf '%s\n' "${_rest#*=}" >"${_rest%%=*}"
done

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
