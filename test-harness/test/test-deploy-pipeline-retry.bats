#!/usr/bin/env bats
# Retry-behavior tests for the deploy pipeline. Drive it end-to-end through a
# minimal example wrapper against mock-wrangler's MOCK_TRANSIENT_FAIL_AT mode,
# which emits classified-retryable or classified-non-retryable error text for
# the first N invocations of a chosen step.
#
# What each test guards against:
#   - fetch_failed_recovers:    transport-layer failure recovers via retry
#                               (today's 2026-04-23 first-failure shape)
#   - code_7429_recovers:       D1 DO-reset split-brain recovers via retry
#                               (today's 2026-04-23 second-failure shape)
#   - edge_503_recovers:        a 5xx rendered from an edge error page, which
#                               carries no `HTTP <status>` token, recovers via
#                               retry (guards against the allowlist accepting
#                               too little - one render of a status must not
#                               classify differently from another)
#   - edge_52x_recovers:        Cloudflare's 52x family in the edge render,
#                               with no reason phrase, recovers via retry
#                               (guards the renders of one status against
#                               drifting apart, and pins the arms to the
#                               status rather than the text after it)
#   - status_lookalike_fails_fast:
#                               digits after an arrow that are not a status
#                               (`-> 520ms`, `-> 5031 rows`) do NOT retry
#                               (guards the status arms' right-hand boundary,
#                               the whole defense against reading a status
#                               out of ordinary tool output)
#   - edge_403_fails_fast:      the same render over a 4xx does NOT retry
#                               (guards the pair: their fixtures differ in
#                               the status and nothing else, so a roster
#                               keying on anything they share - the header
#                               line, a body string - fails this one while
#                               edge_503 still passes)
#   - auth_error_fails_fast:    non-retryable classification does not retry
#                               (guards against the allowlist accepting too
#                               much - auth errors must surface immediately)
#   - exhausted_aborts_clean:   retries are bounded; an always-failing
#                               transient error exits the expected number of
#                               attempts and then follows the same recovery
#                               path a non-retry failure would have taken.
#   - cleanup_rollback_retries: the recovery path's rollback also retries on
#                               transient failure so a flaky API at the
#                               worst possible moment still recovers.
#   - captured_stdout_stays_clean_and_counter_matches:
#                               the callers that parse the runner's
#                               captured stdout (`deployments list` and
#                               `d1 time-travel info`) must not see
#                               retry-diagnostic pollution on a
#                               retry-then-success run, and the diagnostics
#                               bundle's `total_wrangler_retries` field must
#                               equal the actual retry count.

setup() {
  TEST_DIR="$(cd "$(dirname "$BATS_TEST_FILENAME")" && pwd)"
  HARNESS_DIR="$(cd "$TEST_DIR/.." && pwd)"
  WRAPPER="$TEST_DIR/example-deploy-wrapper.sh"
  FIXTURE_DIR="$TEST_DIR/example-fixtures"

  CALLS_LOG="$BATS_TEST_TMPDIR/calls.log"
  : > "$CALLS_LOG"

  export WRANGLER="$HARNESS_DIR/bin/mock-wrangler.sh"
  export FIXTURE_DIR
  export MOCK_LOG_FILE="$CALLS_LOG"
  export MIGRATE_CMD="true"
  export POSTFLIGHT_CMD="true"
  export DEPLOY_DRAIN_SECONDS=0
  # Zero-second backoff keeps retries wall-clock-free in tests. The retry
  # count (= schedule items + 1) is what the test matrix varies, not the
  # sleep duration.
  export DEPLOY_WRANGLER_RETRY_BACKOFF_SECONDS="0 0 0"

  # Real consumers run the wrapper from a directory containing a
  # wrangler config; mirror that here so the pipeline's wrangler-config
  # preflight has something to inspect.
  cd "$BATS_TEST_TMPDIR"
  cat > wrangler.toml <<'EOF'
name = "example"
main = "src/index.ts"
compatibility_date = "2025-01-01"
EOF
}

run_wrapper() {
  for env_kv in "$@"; do
    export "$env_kv"
  done
  run "$WRAPPER" staging --sha smoketest
}

count_step_calls() {
  grep -c "^$1 " "$CALLS_LOG" || true
}

assert_step_called_n_times() {
  expected="$1"
  step="$2"
  actual=$(count_step_calls "$step")
  if [ "$actual" -ne "$expected" ]; then
    echo "expected step '$step' to be called $expected times, got $actual" >&2
    cat "$CALLS_LOG" >&2
    return 1
  fi
}

# ----------------------------------------------------------------------
# Transient fetch-failed on promote_maint: injected failures, then success.
# The retry helper must transparently recover and the rest of the
# pipeline must run as normal.
# ----------------------------------------------------------------------
@test "fetch_failed_recovers: transient network error on promote_maint is retried" {
  run_wrapper MOCK_TRANSIENT_FAIL_AT=promote_maint:2:fetch_failed
  [ "$status" -eq 0 ]
  # One invocation of promote_maint per injected failure, plus the one that
  # succeeds.
  assert_step_called_n_times 3 promote_maint
  # Rest of the happy path still runs after recovery.
  assert_step_called_n_times 1 lock
  assert_step_called_n_times 1 bookmark
  assert_step_called_n_times 1 promote_final
  assert_step_called_n_times 1 unlock
}

# ----------------------------------------------------------------------
# Transient D1 DO-reset (7429) on the lock mutation: injected failures,
# then success. This is the shape that bit the 2026-04-23 rerun - wrangler
# returned non-zero but the UPDATE had already applied server-side.
# Retry converges (the UPDATE is idempotent) and the lock read-back
# verify remains authoritative.
# ----------------------------------------------------------------------
@test "code_7429_recovers: transient D1 reset on lock mutation is retried" {
  run_wrapper MOCK_TRANSIENT_FAIL_AT=lock:2:code_7429
  [ "$status" -eq 0 ]
  assert_step_called_n_times 3 lock
  assert_step_called_n_times 1 bookmark
  assert_step_called_n_times 1 unlock
}

# ----------------------------------------------------------------------
# A Cloudflare 5xx whose body came from the edge rather than the API:
# wrangler cannot parse it into an envelope, so it renders the raw
# exchange with no `HTTP <status>` token anywhere. A roster carrying only
# the envelope render classifies this as non-retryable and burns the
# deploy on a blip - which is what it did to a staging promote, one
# attempt, no retry, after the final version had already uploaded.
# Guarding it on promote_final because that is where it landed: the
# costliest step to lose, since the recovery path has to roll the worker
# back afterwards.
# ----------------------------------------------------------------------
@test "edge_503_recovers: a 503 rendered without an HTTP token is retried" {
  run_wrapper MOCK_TRANSIENT_FAIL_AT=promote_final:2:edge_503
  [ "$status" -eq 0 ]
  assert_step_called_n_times 3 promote_final
  # The promote succeeded on the third attempt, so the recovery path must
  # not have rolled the worker back.
  assert_step_called_n_times 0 wrangler_rollback
  assert_step_called_n_times 1 unlock
}

# ----------------------------------------------------------------------
# Cloudflare's own 52x family in the edge render. The envelope render has
# carried `HTTP 520`-`HTTP 524` since long before the edge arms existed, so
# a roster covering only 500/502/503/504 in the dump left the renders
# of one status disagreeing - the same hole the edge arms were added to
# close, one family over. This fixture also carries no reason phrase,
# which is what proves the arms key on the status rather than the text
# after it.
# ----------------------------------------------------------------------
@test "edge_52x_recovers: a 52x edge render with no reason phrase is retried" {
  run_wrapper MOCK_TRANSIENT_FAIL_AT=promote_final:2:edge_522
  [ "$status" -eq 0 ]
  assert_step_called_n_times 3 promote_final
  assert_step_called_n_times 0 wrangler_rollback
}

# ----------------------------------------------------------------------
# The status arms read a status line, not any digits following an arrow.
# Tool output is full of the latter - a duration, a row count - and the
# right-hand boundary is the whole defense, so this drives a definitive
# config error whose transcript carries `-> 520ms` and `-> 5031 rows` and
# holds it to a single attempt. A boundary loose enough to admit a letter
# retries this one three extra times and then reports the same failure.
# ----------------------------------------------------------------------
@test "status_lookalike_fails_fast: digits after an arrow are not a status" {
  run_wrapper MOCK_TRANSIENT_FAIL_AT=promote_final:99:status_lookalike
  [ "$status" -ne 0 ]
  assert_step_called_n_times 1 promote_final
}

# ----------------------------------------------------------------------
# The mirror of edge_503, and the reason the classifier may not key on the
# malformed-response header: the edge writes that same header over a 403
# or a validation page as readily as over a 503, so a roster matching the
# header alone would retry a definitive verdict. This case's fixture and
# edge_503's differ in the status and nothing else - deliberately, since a
# shared body string would leave it open which arm classified them - so this
# case fails for any roster that reads the render as anything but its status.
# ----------------------------------------------------------------------
@test "edge_403_fails_fast: an unparseable body over a 4xx is not retried" {
  run_wrapper MOCK_TRANSIENT_FAIL_AT=promote_final:99:edge_403
  [ "$status" -ne 0 ]
  assert_step_called_n_times 1 promote_final
}

# ----------------------------------------------------------------------
# Auth errors are outside the retryable allowlist - they must fail on the
# first attempt with no retries. The failure is injected AT promote_maint, so
# the recovery below is a worker rollback: the promote was in flight, and a
# promote that reported failure may still have applied server-side. No bookmark
# exists, so no D1 restore. This guards the allowlist from being accidentally
# broadened to match credentials / config errors.
# ----------------------------------------------------------------------
@test "auth_error_fails_fast: non-retryable classification does not retry" {
  run_wrapper MOCK_TRANSIENT_FAIL_AT=promote_maint:99:auth_error
  [ "$status" -ne 0 ]
  # Exactly one attempt - no retry on a classified non-retryable error.
  assert_step_called_n_times 1 promote_maint
  # Happy-path steps after promote_maint must not run.
  assert_step_called_n_times 0 lock
  assert_step_called_n_times 0 bookmark
  # The promote was in flight when it failed, so the recovery path rolls the
  # worker back rather than assuming the call left nothing behind
  # (fail_promote_maint in the smoke suite owns that invariant). No bookmark
  # exists, so there is no D1 restore.
  assert_step_called_n_times 1 wrangler_rollback
  assert_step_called_n_times 0 tt_restore
}

# ----------------------------------------------------------------------
# Exhausted retry budget behaves like a plain non-retry failure at the
# same point: the final attempt's failure propagates to the caller,
# and the recovery path runs just as it would have under today's
# no-retry behavior. An exhausted promote is exactly the shape the
# in-flight flag exists for - a lost response on the terminal attempt -
# so the worker rollback runs here too.
# ----------------------------------------------------------------------
@test "exhausted_aborts_clean: bounded attempts then propagate failure" {
  # More failures requested than there are attempts, so every attempt
  # fails with fetch_failed and the retry helper returns the last exit code.
  run_wrapper MOCK_TRANSIENT_FAIL_AT=promote_maint:99:fetch_failed
  [ "$status" -ne 0 ]
  # The initial attempt plus one retry per schedule entry.
  assert_step_called_n_times 4 promote_maint
  assert_step_called_n_times 1 wrangler_rollback
  assert_step_called_n_times 0 tt_restore
}

# ----------------------------------------------------------------------
# The recovery path's wrangler rollback call also runs through the retry
# helper: a transient failure at rollback time (the worst possible
# moment for a one-shot miss) still recovers. Drive this by failing at
# lock so the recovery path is forced to run wrangler_rollback, and inject
# transient failures on the rollback step itself.
# ----------------------------------------------------------------------
@test "cleanup_rollback_retries: transient error on recovery rollback is retried" {
  run_wrapper \
    MOCK_FAIL_AT=lock \
    MOCK_TRANSIENT_FAIL_AT=wrangler_rollback:2:fetch_failed
  [ "$status" -ne 0 ]
  # Lock failed once (the MOCK_FAIL_AT path), forcing the recovery path to run.
  assert_step_called_n_times 1 lock
  # One rollback call per injected transient failure, plus the one that
  # succeeds.
  assert_step_called_n_times 3 wrangler_rollback
}

# ----------------------------------------------------------------------
# Invariants (a) and (b) share one scenario because each is observed on the
# same retry-then-success run through the runner:
#   (a) The callers that parse a document out of the runner's stdout
#       (the `deployments_list` and `bookmark_fetch` reads) must see it
#       free of retry-diagnostic text on a retry-then-success path. A
#       regression that let the runner's retry logs reach the returned
#       stdout would break the subsequent parse. Exercising the retry on
#       `deployments_list` drives that path; `bookmark_fetch` uses the
#       same helper and the same caller pattern.
#   (b) `total_wrangler_retries` in the DEPLOY_DIAGNOSTICS_DIR bundle
#       must equal the actual number of retries performed. The counter
#       has no other coverage; an off-by-one increment in the helper
#       would be invisible without this.
# (a) and (b) are proven by running the pipeline to success under
# an injected retry count that jq must survive and the counter must
# reflect exactly.
# ----------------------------------------------------------------------
@test "captured_stdout_stays_clean_and_counter_matches: deployments_list retry succeeds and diagnostics record count" {
  export DEPLOY_DIAGNOSTICS_DIR="$BATS_TEST_TMPDIR/diagnostics"
  run_wrapper MOCK_TRANSIENT_FAIL_AT=deployments_list:2:fetch_failed
  # Pipeline must complete - implies the deployments_list parse succeeded despite
  # the prior retry cycles on the same call.
  [ "$status" -eq 0 ]
  assert_step_called_n_times 3 deployments_list

  diagnostics_file="$DEPLOY_DIAGNOSTICS_DIR/deploy-diagnostics.txt"
  [ -f "$diagnostics_file" ]
  # The counter must equal the injected retries, not merely reach them -
  # an off-by-one (e.g. incrementing on the first attempt or the success
  # attempt) would fail this assertion.
  grep -q '^total_wrangler_retries=2$' "$diagnostics_file"
}

# ----------------------------------------------------------------------
# The upload steps replay every attempt's stderr so operators can read the
# full CI transcript, but the Worker Version ID read MUST target only the
# terminal attempt's output - not the full transcript - or correctness
# depends on silent couplings (retry diagnostic lines not matching
# `Worker Version ID:`; prior attempts not emitting that pattern). This
# test drives transient failures on upload_maint so the runner returns
# a terminal-attempt result distinct from the transcript, and the version
# ID taken from it is the one used downstream.
#
# What the assertion rests on: mock-wrangler classifies a `versions deploy`
# call as `promote_maint` only when the version-id argument contains the
# maintenance slot UUID (11111111, per lib/test-slots.sh - see
# `*"versions deploy"*"11111111"*` in mock-wrangler.sh). A wrong grep
# would hand an unrelated string to `versions deploy`, classify as
# `other`, and the promote_maint count would be 0. Getting 1 proves the
# grep picked the right UUID out of the terminal attempt.
# ----------------------------------------------------------------------
@test "upload_maint_retry_keeps_right_version_id: terminal attempt's ID is what gets promoted" {
  run_wrapper MOCK_TRANSIENT_FAIL_AT=upload_maint:2:fetch_failed
  [ "$status" -eq 0 ]
  assert_step_called_n_times 3 upload_maint
  assert_step_called_n_times 1 promote_maint
}
