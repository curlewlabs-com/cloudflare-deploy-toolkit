# shellcheck shell=sh
#
# Sourceable helper functions for consumer wrangler-fixture capture scripts.
#
# Usage:
#   . "$TOOLKIT/test-harness/lib/capture-helpers.sh"
#   capture_deployments_list staging > fixtures/deployments_list.json
#   capture_versions_upload_dry_run staging "fixture-capture maintenance" \
#     --var "MAINTENANCE_MODE:true" \
#     | "$SANITIZER" upload-maint --scrub-binding ACTIVITY_CHECK_TEST_UID \
#     > fixtures/upload_maint.txt
#
# Each helper wraps a single wrangler subcommand with the appropriate safety
# flags (--dry-run where the subcommand supports it, --json where it returns
# structured data) and writes raw output to stdout. The consumer pipes the
# raw output through `bin/sanitize-wrangler-text.sh` and/or jq to scrub PII
# and pin variability before committing.
#
# Why a sourceable library and not standalone scripts: each consumer's capture
# script needs the SAME wrangler invocation patterns but DIFFERENT post-
# processing (different binding scrubs, different fixture names, different
# additional fields). Library functions let consumers compose without
# duplicating wrangler-flag knowledge.
#
# Safety guarantee: every helper here is either read-only or uses --dry-run.
# No helper in this file can mutate the target environment. Helpers that
# could mutate (e.g. d1 execute, d1 time-travel restore) deliberately
# DO NOT exist - those steps' fixtures are empty marker files in the
# consumer's fixture dir.
#
# All helpers take WRANGLER_ENV (staging/production) as first argument and
# verify it is "staging" before running. Capturing against production is
# always a bug.
#
# This library sets no shell options: errexit/nounset are owned by the
# executable that sources it, so setting them here would leak into the caller.

WRANGLER_HARNESS_BIN="${WRANGLER_HARNESS_BIN:-bunx wrangler}"

# Every $WRANGLER_HARNESS_BIN call below is prefixed with CI=true to suppress
# wrangler's "Cloudflare agent skills" banner, which prints to stdout under
# agent runtimes (e.g. Claude Code) and would corrupt the --json output the
# consumer pipes through jq (deployments list, time-travel info) or the
# dry-run text the sanitizer parses. This library is sourced, so it must not
# export CI (that would leak into the consumer's shell and its own hooks);
# the prefix is scoped to each external wrangler process instead.

# Verify the env is staging. Production capture would risk creating real
# deployments and exposes prod metadata in committed fixtures.
_assert_staging_env() {
    fn="$1"
    env="$2"
    if [ "$env" != "staging" ]; then
        echo "$fn: refusing to capture against '$env' - staging only" >&2
        return 1
    fi
}

# capture_deployments_list <env>
# Calls `wrangler deployments list --json` and writes the raw JSON to stdout.
# Read-only - wrangler does not mutate when listing deployments.
capture_deployments_list() {
    _assert_staging_env capture_deployments_list "$1" || return 1
    env="$1"
    # shellcheck disable=SC2086
    CI=true $WRANGLER_HARNESS_BIN deployments list --json --env "$env"
}

# capture_d1_time_travel_info <env> <db-name>
# Calls `wrangler d1 time-travel info --json` and writes the raw JSON to
# stdout. Read-only - time-travel info just returns the current bookmark.
capture_d1_time_travel_info() {
    _assert_staging_env capture_d1_time_travel_info "$1" || return 1
    env="$1"
    db_name="$2"
    if [ -z "$db_name" ]; then
        echo "capture_d1_time_travel_info: db_name required" >&2
        return 1
    fi
    # shellcheck disable=SC2086
    CI=true $WRANGLER_HARNESS_BIN d1 time-travel info "$db_name" --env "$env" --json
}

# capture_versions_upload_dry_run <env> <message> [extra wrangler args ...]
# Calls `wrangler versions upload --dry-run` and writes combined stdout+stderr
# to stdout. The dry-run flag means no version is created - wrangler compiles
# the bundle and exits without uploading. The output contains the binding
# list (most likely thing to drift between wrangler versions); the
# `Worker Version ID:` line is NOT present in dry-run output, so consumers
# must pipe through sanitize-wrangler-text.sh in upload-maint or upload-final
# mode to inject the synthetic line.
#
# Pass extra wrangler args (e.g. `--var MAINTENANCE_MODE:true`) after the
# message arg.
capture_versions_upload_dry_run() {
    _assert_staging_env capture_versions_upload_dry_run "$1" || return 1
    env="$1"
    message="$2"
    shift 2
    # shellcheck disable=SC2086
    CI=true $WRANGLER_HARNESS_BIN versions upload --env "$env" --dry-run \
        --message "$message" \
        "$@" 2>&1
}

# capture_triggers_deploy_dry_run <env>
# Calls `wrangler triggers deploy --dry-run` and writes combined stdout+stderr
# to stdout. The dry-run flag means no schedules or routes are pushed.
# Output contains an experimental-warning banner that's stable across
# wrangler versions (changes only when Cloudflare promotes triggers deploy
# out of experimental status - that's a real signal worth catching).
capture_triggers_deploy_dry_run() {
    _assert_staging_env capture_triggers_deploy_dry_run "$1" || return 1
    env="$1"
    # shellcheck disable=SC2086
    CI=true $WRANGLER_HARNESS_BIN triggers deploy --env "$env" --dry-run 2>&1
}

# write_empty_fixtures <fixture-dir> <step1> <step2> ...
# Creates empty marker files for steps where wrangler's output is not parsed
# by any consumer. The mock just needs `cat` to succeed; the existence of
# the file documents that the step was considered. Use for: lock, unlock,
# triggers (if you want a marker instead of a captured fixture), promote_*,
# tt_restore, wrangler_rollback.
write_empty_fixtures() {
    fixture_dir="$1"
    shift
    if [ ! -d "$fixture_dir" ]; then
        echo "write_empty_fixtures: fixture dir does not exist: $fixture_dir" >&2
        return 1
    fi
    for step in "$@"; do
        : >"$fixture_dir/$step.txt"
    done
}

# reshape_deployments_list_json <input-json-file> <latest-uuid> <older-uuid> <test-timestamp> <test-email>
# Reads a wrangler deployments list --json file with latest and prior entries,
# takes that trailing pair, and writes a fully-normalized fixture array to stdout where:
#   - .[1].versions[0].version_id = <latest-uuid>  (the .[-1] target)
#   - .[0].versions[0].version_id = <older-uuid>   (the second-to-latest)
#   - id, author_email, annotations, created_on are pinned to fixed values
#
# The OUTER schema (id/source/strategy/annotations/versions/created_on) is
# preserved so a future wrangler schema change surfaces in `git diff` on
# re-capture. Only the variable VALUES are pinned. The `source` and
# `strategy` fields pass through from the corresponding input element so a
# wrangler change in those values still surfaces.
#
# The annotations object is hard-pinned (not preserved) because wrangler
# emits the deploy SHA inside `workers/message`, which would leak the
# capturer's commit into committed fixtures. Override the default with
# WRANGLER_HARNESS_DEPLOYMENTS_ANNOTATIONS - the value must be a valid JSON
# object literal (jq --argjson is used so the contents are parsed, not
# string-quoted).
#
# Why a helper and not inline jq in each consumer: every consumer needs the
# same .[-2:] reshape with the same slot-UUID mapping (LATEST at .[-1],
# OLDER at .[0]). Only the slot UUID values themselves are
# consumer-controlled, so they're positional args. Lifting this out also
# gives the regression a single test surface - the .[0] vs .[-1] swap that
# motivated extracting this helper is now caught by test-capture-helpers.bats.
reshape_deployments_list_json() {
    input="$1"
    latest_uuid="$2"
    older_uuid="$3"
    test_timestamp="$4"
    test_email="$5"

    if [ -z "$input" ] || [ -z "$latest_uuid" ] || [ -z "$older_uuid" ] || [ -z "$test_timestamp" ] || [ -z "$test_email" ]; then
        echo "reshape_deployments_list_json: usage: reshape_deployments_list_json <input-json-file> <latest-uuid> <older-uuid> <test-timestamp> <test-email>" >&2
        return 1
    fi

    if [ ! -f "$input" ]; then
        echo "reshape_deployments_list_json: input file does not exist: $input" >&2
        return 1
    fi

    # Set the default outside the parameter expansion: `${VAR:-{...}}` doesn't
    # work with literal braces inside the default value (the inner `}` closes
    # the expansion).
    if [ -n "${WRANGLER_HARNESS_DEPLOYMENTS_ANNOTATIONS:-}" ]; then
        annotations_json="$WRANGLER_HARNESS_DEPLOYMENTS_ANNOTATIONS"
    else
        annotations_json='{"workers/message":"fixture","workers/triggered_by":"deployment"}'
    fi

    # Verifies the input is a JSON array AND has latest and prior elements in one
    # call. `jq length` errors out non-zero on non-arrays, so the explicit
    # arithmetic check below only ever sees integers.
    count=$(jq 'length' <"$input") || {
        echo "reshape_deployments_list_json: input is not valid JSON or not an array: $input" >&2
        return 1
    }
    if [ "$count" -lt 2 ]; then
        echo "reshape_deployments_list_json: input has $count deployments; need at least 2 to fixture .[-1] vs .[0] semantics" >&2
        return 1
    fi

    # The reshape writes each of the pair as one version at 100%, so it must
    # read one: an entry with no versions, or a split across versions, would
    # otherwise come out as a well-formed fixture and hide the very schema
    # drift or gradual deployment a re-capture is meant to surface.
    if ! jq -e '.[-2:] | all(
        type == "object"
        and (.versions | type) == "array"
        and (.versions | length) == 1
        and (.versions[0].version_id | type) == "string"
        and (.versions[0].version_id | length) > 0
        and .versions[0].percentage == 100
    )' <"$input" >/dev/null; then
        echo "reshape_deployments_list_json: the last two deployments in $input must each serve one version at 100%" >&2
        return 1
    fi

    jq --arg older "$older_uuid" \
        --arg latest "$latest_uuid" \
        --arg ts "$test_timestamp" \
        --arg email "$test_email" \
        --argjson annotations "$annotations_json" \
        '.[-2:] | [
       {
         id: "00000000-0000-0000-0000-aaaaaaaaaaaa",
         source: .[0].source,
         strategy: .[0].strategy,
         author_email: $email,
         annotations: $annotations,
         versions: [{ version_id: $older, percentage: 100 }],
         created_on: $ts
       },
       {
         id: "00000000-0000-0000-0000-bbbbbbbbbbbb",
         source: .[1].source,
         strategy: .[1].strategy,
         author_email: $email,
         annotations: $annotations,
         versions: [{ version_id: $latest, percentage: 100 }],
         created_on: $ts
       }
     ]' <"$input"
}
