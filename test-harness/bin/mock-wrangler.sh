#!/bin/sh
# Mock wrangler binary used by consumer test drivers.
#
# - Logs every invocation (subcommand + args) to $MOCK_LOG_FILE.
# - Reads MOCK_FAIL_AT to inject failures at named steps.
# - Returns canned output read from $FIXTURE_DIR/<step>.{json,txt}.
#
# This script is invoked via $WRANGLER from a consumer's deploy script - the
# test driver sets WRANGLER=$(absolute path to this script). $WRANGLER is
# intentionally word-split in deploy scripts ("npx wrangler"), so the path
# here must not contain spaces.
#
# Required env vars:
#   MOCK_LOG_FILE - appended-to with one line per call: "$STEP $*"
#   FIXTURE_DIR   - directory containing per-step fixture files
#
# Optional env vars:
#   MOCK_FAIL_AT  - step name, or several separated by whitespace; the mock
#                   exits 73 when any of them is invoked
#   MOCK_TRANSIENT_FAIL_AT - "<step>:<count>:<pattern>" - fails the first
#                      <count> invocations of <step> with stderr text that
#                      matches <pattern> (chosen to be classified as
#                      transient or not by the toolkit's retry
#                      classifier). After <count> failures, subsequent
#                      invocations succeed normally. Patterns:
#                        fetch_failed  - "[ERROR] fetch failed" (retryable)
#                        version_not_found - the version endpoint's not-found,
#                                         "[code: 100146]" (retryable only on
#                                         a read-back of a fresh upload)
#                        code_7429     - "[ERROR] D1 DB storage operation
#                                         exceeded timeout ... [code: 7429]"
#                                         (retryable)
#                        http_429      - "[ERROR] HTTP 429 Too Many Requests"
#                                         (retryable)
#                        http_503      - "[ERROR] HTTP 503 Service
#                                         Unavailable" (retryable)
#                        edge_503      - the same 503 as wrangler renders it
#                                         when the body came from the edge and
#                                         not the API, so there is no
#                                         `HTTP <status>` token to match
#                                         (retryable)
#                        edge_522      - a 52x edge render, status only, no
#                                         reason phrase (retryable)
#                        status_lookalike
#                                      - a config error whose output carries
#                                         digits after an arrow (`-> 520ms`),
#                                         which is not a status line
#                                         (NOT retryable)
#                        edge_403      - that same render over a 403, differing
#                                         from edge_503 in the status and
#                                         nothing else (NOT retryable)
#                        auth_error    - "[ERROR] 10000: Authentication error"
#                                         (NOT retryable by the classifier)
#                      Exit code is 1 for transient failures, 10 for auth
#                      (each distinct from MOCK_FAIL_AT's 73 so the
#                      modes don't silently converge).
#   MOCK_PER_DB_LOCK_STATE - when 1, each database named after `d1 execute`
#                      keeps its own lock state, in "$MOCK_LOG_FILE.lock_state.
#                      <name>" (a test seeds a database's initial state by
#                      writing that file; unseeded ones start from
#                      MOCK_INITIAL_LOCK_STATE). A relocation has a source and
#                      a target, and one shared state cannot hold a fenced
#                      source beside a target that was never filled.
#   MOCK_SERVING_AFTER_PROMOTE - what `deployments list` reports once a final
#                      promote has returned, in place of the promoted version: a
#                      version id, or `split` for a gradual deployment across
#                      versions. Stands in for a promote that returned success
#                      and did not leave its version the single one serving.
#   MOCK_VERSION_MESSAGE - the `workers/message` annotation `versions view`
#                      reports, in place of the versions_view fixture's. Stands
#                      in for a serving version uploaded from another build, or
#                      outside the pipeline.
#   MOCK_DELAY_AT - "<step>:<seconds>": every invocation of <step> takes that
#                      long. Stands in for a Cloudflare call that is slow rather
#                      than failing, which is what a time budget has to survive.
#   MOCK_LOCK_STATE_AT - "<step>:<database>:<state>": when <step> is invoked,
#                      <database>'s lock state becomes <state> (needs
#                      MOCK_PER_DB_LOCK_STATE). Stands in for another actor
#                      taking a lock while a deploy is part-way through.
#
# Steps the mock recognizes (these are the names that go in MOCK_FAIL_AT
# and the basenames of fixture files):
#   deployments_list, versions_view, upload_maint, upload_final, triggers,
#   promote_maint, promote_final, lock, lock_state, unlock, bookmark,
#   tt_restore, wrangler_rollback - the calls the pipeline itself makes. A
#   consumer's own hook queries are its own to stub; anything else is `other`
#   and emits nothing.
#
# Why this mock and not bats-mock: bats-mock provides PATH-injection and call
# recording, but does not handle fixture-file replay or named-step failure
# injection. mock-wrangler.sh layers those on top - the test driver is a
# `.bats` file but the mock binary stays as a free-standing shell script
# so consumers can debug it from the command line.

set -eu

if [ -z "${MOCK_LOG_FILE:-}" ]; then
    echo "mock-wrangler: MOCK_LOG_FILE env var not set" >&2
    exit 99
fi

if [ -z "${FIXTURE_DIR:-}" ]; then
    echo "mock-wrangler: FIXTURE_DIR env var not set" >&2
    echo "  Consumers must set FIXTURE_DIR to their captured fixture directory." >&2
    echo "  See test-harness/README.md in the toolkit" >&2
    exit 99
fi

if [ ! -d "$FIXTURE_DIR" ]; then
    echo "mock-wrangler: fixture dir not found at $FIXTURE_DIR" >&2
    exit 99
fi

LOCK_STATE_FILE="$MOCK_LOG_FILE.lock_state"
if [ "${MOCK_PER_DB_LOCK_STATE:-}" = "1" ] && [ "${1:-} ${2:-}" = "d1 execute" ] && [ -n "${3:-}" ]; then
    LOCK_STATE_FILE="$LOCK_STATE_FILE.$3"
fi
if [ ! -f "$LOCK_STATE_FILE" ]; then
    printf '%s\n' "${MOCK_INITIAL_LOCK_STATE:-unlocked}" >"$LOCK_STATE_FILE"
fi
LOCK_STATE=$(cat "$LOCK_STATE_FILE")
PROMOTED_FILE="$MOCK_LOG_FILE.promoted"
# Classify the invocation by inspecting all args. The classification is
# generic - it matches against wrangler subcommand patterns rather than any
# consumer-specific naming. The slot UUIDs (11111111 / 22222222) come from
# lib/test-slots.sh and are replicated here in the case patterns because
# `case` doesn't expand variables in patterns.
case "$*" in
*"deployments list"*"--json"*)
    STEP=deployments_list
    ;;
*"versions view"*)
    STEP=versions_view
    ;;
*"versions upload"*"MAINTENANCE_MODE:true"*)
    STEP=upload_maint
    ;;
*"versions upload"*)
    STEP=upload_final
    ;;
*"triggers deploy"*)
    STEP=triggers
    ;;
*"versions deploy"*"11111111"*)
    STEP=promote_maint
    ;;
*"versions deploy"*"22222222"*)
    STEP=promote_final
    ;;
*"d1 execute"*"SELECT locked FROM _deploy_lock WHERE id = 1"* | *"d1 execute"*"SELECT id, locked FROM _deploy_lock ORDER BY id;"*)
    STEP=lock_state
    ;;
*"d1 execute"*"locked = 1"*)
    STEP=lock
    ;;
*"d1 execute"*"locked = 0"*)
    STEP=unlock
    ;;
*"d1 time-travel info"*)
    STEP=bookmark
    ;;
*"d1 time-travel restore"*)
    STEP=tt_restore
    ;;
*"rollback"*)
    STEP=wrangler_rollback
    ;;
*)
    STEP=other
    ;;
esac

echo "$STEP $*" >>"$MOCK_LOG_FILE"

if [ -n "${MOCK_DELAY_AT:-}" ] && [ "${MOCK_DELAY_AT%%:*}" = "$STEP" ]; then
    sleep "${MOCK_DELAY_AT#*:}"
fi

if [ -n "${MOCK_LOCK_STATE_AT:-}" ] && [ "${MOCK_LOCK_STATE_AT%%:*}" = "$STEP" ]; then
    _mock_rest="${MOCK_LOCK_STATE_AT#*:}"
    printf '%s\n' "${_mock_rest#*:}" >"$MOCK_LOG_FILE.lock_state.${_mock_rest%%:*}"
fi

# Inject failure if requested. Exit 73 is arbitrary but distinct from any real
# wrangler exit code we care about, making mock-injected failures easy to spot
# in test output.
# Unquoted on purpose: the value is a step name, or several separated by
# whitespace, and a recovery case needs more than one - a promote that fails and
# then a rollback that fails too is the state where the pipeline has lost track
# of which version is serving.
# shellcheck disable=SC2086
for _mock_fail_step in ${MOCK_FAIL_AT:-}; do
    [ "$STEP" = "$_mock_fail_step" ] || continue
    case "${MOCK_FAIL_SIDE_EFFECT_LOCK_STATE:-}" in
    locked | unlocked | missing)
        printf '%s\n' "$MOCK_FAIL_SIDE_EFFECT_LOCK_STATE" >"$LOCK_STATE_FILE"
        ;;
    esac
    echo "mock-wrangler: injected failure at step=$STEP" >&2
    exit 73
done

# Transient-failure injection for retry-behavior tests. A per-run counter
# file tracks how many failures remain for the configured step; each
# matching call decrements and emits the chosen error pattern on stderr.
# After the count reaches zero, subsequent calls of that step fall through
# to the normal success-path fixture replay below.
if [ -n "${MOCK_TRANSIENT_FAIL_AT:-}" ]; then
    _mt_step=$(printf '%s' "$MOCK_TRANSIENT_FAIL_AT" | cut -d: -f1)
    _mt_count=$(printf '%s' "$MOCK_TRANSIENT_FAIL_AT" | cut -d: -f2)
    _mt_pattern=$(printf '%s' "$MOCK_TRANSIENT_FAIL_AT" | cut -d: -f3)
    if [ -z "$_mt_step" ] || [ -z "$_mt_count" ] || [ -z "$_mt_pattern" ]; then
        echo "mock-wrangler: MOCK_TRANSIENT_FAIL_AT must be '<step>:<count>:<pattern>', got '$MOCK_TRANSIENT_FAIL_AT'" >&2
        exit 99
    fi
    if [ "$STEP" = "$_mt_step" ]; then
        _mt_state_file="$MOCK_LOG_FILE.transient_count"
        if [ ! -f "$_mt_state_file" ]; then
            printf '%s\n' "$_mt_count" >"$_mt_state_file"
        fi
        _mt_remaining=$(cat "$_mt_state_file")
        if [ "$_mt_remaining" -gt 0 ]; then
            _mt_remaining=$((_mt_remaining - 1))
            printf '%s\n' "$_mt_remaining" >"$_mt_state_file"
            case "$_mt_pattern" in
            fetch_failed)
                echo "[ERROR] fetch failed" >&2
                exit 1
                ;;
            version_not_found)
                echo "[ERROR] A request to the Cloudflare API failed." >&2
                echo "  The requested Worker version could not be found, please check the ID being passed and try again. [code: 100146]" >&2
                exit 1
                ;;
            code_7429)
                echo "[ERROR] A request to the Cloudflare API failed." >&2
                echo "  D1 DB storage operation exceeded timeout which caused object to be reset. [code: 7429]" >&2
                exit 1
                ;;
            http_429)
                echo "[ERROR] HTTP 429 Too Many Requests" >&2
                exit 1
                ;;
            http_503)
                echo "[ERROR] HTTP 503 Service Unavailable" >&2
                exit 1
                ;;
            edge_503)
                # The render of a live CI failure, with the account, script and
                # ray identifiers replaced. Wrangler emits this instead of the
                # http_503 render whenever the 503 body is the edge's error page
                # rather than the API's JSON envelope, so it carries no
                # `HTTP <status>` token at all - the shape that has to keep
                # reaching the classifier.
                #
                # The live instance also carried a proxy `upstream connect
                # error` line, dropped here: that string is its own transport
                # token in the roster, so a fixture holding that line too could not show
                # which arm classified it. Without it this differs from edge_403
                # in the status alone, which is the invariant the pair holds.
                echo "[ERROR] Received a malformed response from the API" >&2
                echo "" >&2
                echo "  <html><head><title>503 Service Unavailable</title></head></html>" >&2
                echo "  GET /accounts/00000000000000000000000000000000/workers/scripts/example-worker/deployments -> 503 Service Unavailable" >&2
                echo "  Cloudflare Ray ID: 0000000000000000-AAA" >&2
                exit 1
                ;;
            edge_522)
                # A 52x edge render. The status line carries no reason phrase:
                # Cloudflare's 52x phrases are not ones this repo has an
                # instance of, and inventing one here would put a string in the
                # fixture that no real transcript has to match. The classifier
                # reads the status, so the omission costs the test nothing and
                # exercises the reason-less form of the line besides.
                echo "[ERROR] Received a malformed response from the API" >&2
                echo "" >&2
                echo "  GET /accounts/00000000000000000000000000000000/workers/scripts/example-worker/deployments -> 522" >&2
                echo "  Cloudflare Ray ID: 0000000000000000-AAA" >&2
                exit 1
                ;;
            status_lookalike)
                # A definitive failure whose output happens to carry digits
                # after an arrow - a duration here, which is the shape that
                # actually occurs. Nothing in it is a status line, so the
                # classifier must not read one out of it.
                echo "  Uploaded example-worker -> 520ms" >&2
                echo "  Bindings resolved -> 5031 rows" >&2
                echo "[ERROR] Processing wrangler configuration: binding DB refers to a D1 database that does not exist" >&2
                exit 1
                ;;
            edge_403)
                # Same unparseable-body render as edge_503, over a definitive
                # verdict. The header line is identical, so this is what proves
                # the classifier reads the status and not the header.
                echo "[ERROR] Received a malformed response from the API" >&2
                echo "" >&2
                echo "  <html><head><title>403 Forbidden</title></head></html>" >&2
                echo "  GET /accounts/00000000000000000000000000000000/workers/scripts/example-worker/deployments -> 403 Forbidden" >&2
                echo "  Cloudflare Ray ID: 0000000000000000-AAA" >&2
                exit 1
                ;;
            auth_error)
                echo "[ERROR] 10000: Authentication error" >&2
                exit 10
                ;;
            *)
                echo "mock-wrangler: unknown MOCK_TRANSIENT_FAIL_AT pattern '$_mt_pattern'" >&2
                exit 99
                ;;
            esac
        fi
    fi
fi

case "$STEP" in
lock)
    printf '%s\n' "locked" >"$LOCK_STATE_FILE"
    ;;
unlock)
    printf '%s\n' "unlocked" >"$LOCK_STATE_FILE"
    ;;
promote_final)
    # The serving set follows a promote, so a read made after one sees what was
    # promoted rather than the fixture's fixed answer.
    printf '%s' "$*" | grep -Eo '[0-9a-f-]{36}@100%' | head -n 1 | cut -d@ -f1 >"$PROMOTED_FILE"
    ;;
esac

LOCK_STATE=$(cat "$LOCK_STATE_FILE")

# Cat the fixture file for this step. JSON-shaped fixtures (parsed by jq in
# consumer deploy scripts) get the .json extension; everything else is .txt.
# Steps that have no parsed output (lock, unlock, triggers, promote_*,
# tt_restore, wrangler_rollback) have empty .txt fixture files in the
# consumer's fixture dir - the cat is still real, it just emits nothing,
# mirroring wrangler's quiet-success behavior for these commands.
case "$STEP" in
deployments_list)
    if [ -s "$PROMOTED_FILE" ] && [ "${MOCK_SERVING_AFTER_PROMOTE:-}" = "split" ]; then
        printf '[{"id":"00000000-0000-0000-0000-cccccccccccc","versions":[{"version_id":"%s","percentage":50},{"version_id":"00000000-0000-0000-0000-000000000000","percentage":50}]}]\n' "$(cat "$PROMOTED_FILE")"
    elif [ -s "$PROMOTED_FILE" ]; then
        printf '[{"id":"00000000-0000-0000-0000-cccccccccccc","versions":[{"version_id":"%s","percentage":100}]}]\n' "${MOCK_SERVING_AFTER_PROMOTE:-$(cat "$PROMOTED_FILE")}"
    else
        cat "$FIXTURE_DIR/$STEP.json"
    fi
    ;;
versions_view)
    if [ -n "${MOCK_VERSION_MESSAGE+set}" ]; then
        jq -n --arg message "$MOCK_VERSION_MESSAGE" '{annotations: {"workers/message": $message}}'
    else
        cat "$FIXTURE_DIR/$STEP.json"
    fi
    ;;
bookmark)
    cat "$FIXTURE_DIR/$STEP.json"
    ;;
upload_maint | upload_final | triggers | promote_maint | promote_final | lock | unlock | tt_restore | wrangler_rollback)
    cat "$FIXTURE_DIR/$STEP.txt"
    ;;
lock_state)
    case "$LOCK_STATE" in
    locked)
        printf '%s\n' '[{"results":[{"id":1,"locked":1}]}]'
        ;;
    unlocked)
        printf '%s\n' '[{"results":[{"id":1,"locked":0}]}]'
        ;;
    missing)
        printf '%s\n' '{"error":{"text":"no such table: _deploy_lock: SQLITE_ERROR"}}'
        exit 1
        ;;
    *)
        echo "mock-wrangler: unknown lock state '$LOCK_STATE'" >&2
        exit 99
        ;;
    esac
    ;;
other)
    : # unknown wrangler subcommand - emit nothing
    ;;
esac
