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
#   MOCK_SERVING_AFTER_PROMOTE - what `deployments list` reports once a final
#                      promote has returned, in place of the promoted version: a
#                      version id, or `split` for a gradual deployment across
#                      versions. Stands in for a promote that returned success
#                      and did not leave its version the single one serving.
#   MOCK_VERSION_MESSAGE - the `workers/message` annotation `versions view`
#                      reports, in place of the versions_view fixture's. Stands
#                      in for a serving version uploaded from another build, or
#                      outside the pipeline.
#
# Named steps correspond to the Wrangler operations in the case below.
# Unrecognized hook calls are recorded as other.
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

PROMOTED_FILE="$MOCK_LOG_FILE.promoted"
# A promotion is recognized only when it spends the uploaded fixture's ID.
case "$*" in
*"deployments list"*"--json"*)
    STEP=deployments_list
    ;;
*"versions view"*)
    STEP=versions_view
    ;;
*"versions upload"*)
    STEP=upload_final
    ;;
*"triggers deploy"*)
    STEP=triggers
    ;;
*"versions deploy"*"22222222"*)
    STEP=promote_final
    ;;
*"rollback"*)
    STEP=wrangler_rollback
    ;;
*)
    STEP=other
    ;;
esac

echo "$STEP $*" >>"$MOCK_LOG_FILE"

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
            upload_id_then_fetch_failed)
                # A failed attempt can still have emitted a syntactically valid ID.
                echo "Worker Version ID: aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
                echo "[ERROR] fetch failed" >&2
                exit 1
                ;;
            fetch_failed)
                echo "[ERROR] fetch failed" >&2
                exit 1
                ;;
            post_promote_error | submission_not_found)
                # Wrangler can print the annotation on stdout and then fail
                # after changing traffic, while finishing a different API call.
                printf '%s\n' "${3%@100%}" >"$PROMOTED_FILE"
                if [ "$_mt_pattern" = submission_not_found ]; then
                    echo "Version message: fetch failed"
                    echo "[ERROR] A request to the Cloudflare API (/accounts/example/workers/scripts/worker/deployments) failed." >&2
                    echo "  The requested Worker version could not be found. [code: 100146]" >&2
                    exit 1
                fi
                echo "Version message: [code: 100146]"
                echo "[ERROR] A request to the Cloudflare API for deployment metadata failed." >&2
                exit 1
                ;;
            version_not_found | version_not_found_superseded | version_not_found_with_annotation | version_not_found_unreadable)
                if [ "$_mt_pattern" = version_not_found_with_annotation ]; then
                    echo "Version message: fetch failed"
                elif [ "$_mt_pattern" = version_not_found_unreadable ]; then
                    : >"$MOCK_LOG_FILE.unreadable_deployments"
                fi
                if [ "$_mt_pattern" = version_not_found_superseded ]; then
                    # Another controller took traffic during the rejected promotion.
                    printf '%s\n' "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa" >"$PROMOTED_FILE"
                fi
                [ "${WRANGLER_LOG:-}" != none ] || exit 1
                echo "[ERROR] A request to the Cloudflare API (/accounts/example/workers/scripts/worker/versions/${3%@100%}) failed." >&2
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
promote_final)
    # The serving set follows a promote, so a read made after one sees what was
    # promoted rather than the fixture's fixed answer.
    printf '%s' "$*" | grep -Eo '[0-9a-f-]{36}@100%' | head -n 1 | cut -d@ -f1 >"$PROMOTED_FILE"
    ;;
esac

# Cat the fixture file for this step. JSON-shaped fixtures (parsed by jq in
# consumer deploy scripts) get the .json extension; everything else is .txt.
case "$STEP" in
deployments_list)
    if [ -f "$MOCK_LOG_FILE.unreadable_deployments" ]; then
        echo "[ERROR] Cannot read the serving set" >&2
        exit 1
    fi
    if [ -n "${MOCK_AFTER_TRIGGERS_DEPLOYMENTS:-}" ] && grep -q '^triggers ' "$MOCK_LOG_FILE"; then
        cat "$MOCK_AFTER_TRIGGERS_DEPLOYMENTS"
    elif [ -s "$PROMOTED_FILE" ] && [ "${MOCK_SERVING_AFTER_PROMOTE:-}" = "split" ]; then
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
upload_final | triggers | promote_final | wrangler_rollback)
    cat "$FIXTURE_DIR/$STEP.txt"
    ;;
other)
    : # unknown wrangler subcommand - emit nothing
    ;;
esac
