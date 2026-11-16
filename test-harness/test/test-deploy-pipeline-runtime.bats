#!/usr/bin/env bats
# The uploaded-runtime check (DEPLOY_EXPECTED_RUNTIME), through the example
# wrapper and the mock wrangler.
#
# A consumer's tests run at the runtime it configures, so an upload Cloudflare
# records at any other must be refused before it serves - and before any window
# or write pause opens, so a slow or failed read holds nothing paused. Each
# refusal names its cause, because only a mismatch is a statement about the
# runtime. The versions_view fixture records 2025-01-01 and no flags. The
# relocation body's case lives with the relocation suite, which owns its lock
# setup.

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
  # Substitute the consumer's hook bodies with shell builtins so the
  # test doesn't need a real D1 / bun / migration system.
  export MIGRATE_CMD="true"
  export POSTFLIGHT_CMD="true"
  # Skip the real 15s in-flight drain sleep. mock-wrangler is
  # deterministic and has no in-flight requests to drain, so the sleep
  # adds no coverage - it just burns wall time in every case that
  # reaches the drain step.
  export DEPLOY_DRAIN_SECONDS=0

  # Real consumers run the wrapper from a directory containing a
  # wrangler config; mirror that here so the pipeline's wrangler-config
  # preflight has something to inspect. Tests that exercise the
  # preflight failure modes overwrite this file or remove it.
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

assert_step_called() {
  if ! grep -q "^$1 " "$CALLS_LOG"; then
    echo "expected step '$1' to be in call log, but it wasn't:" >&2
    cat "$CALLS_LOG" >&2
    return 1
  fi
}

refute_step_called() {
  if grep -q "^$1 " "$CALLS_LOG"; then
    echo "expected step '$1' to NOT be in call log, but it was:" >&2
    cat "$CALLS_LOG" >&2
    return 1
  fi
}

MATCHING_RUNTIME='{"compatibility_date": "2025-01-01", "compatibility_flags": []}'

line_of() {
  grep -n "$1" "$CALLS_LOG" | head -n 1 | cut -d: -f1
}

@test "expected_runtime_unset: no runtime read on any body, so other consumers are untouched" {
  # A consumer that never sets it - an older target, another product - must
  # see no extra call and no behavior change.
  unset DEPLOY_EXPECTED_RUNTIME
  run_wrapper
  [ "$status" -eq 0 ]
  refute_step_called versions_view
  [[ "$output" != *"Verify the uploaded version's runtime"* ]]
  : > "$CALLS_LOG"
  run_wrapper DEPLOY_PIPELINE_MODE=windowless
  [ "$status" -eq 0 ]
  refute_step_called versions_view
}

@test "expected_runtime_matches_windowed: the maintenance upload is read back before the triggers and the promote" {
  run_wrapper "DEPLOY_EXPECTED_RUNTIME=$MATCHING_RUNTIME"
  [ "$status" -eq 0 ]
  [ "$(line_of '^upload_maint ')" -lt "$(line_of '^versions_view ')" ]
  [ "$(line_of '^versions_view ')" -lt "$(line_of '^triggers ')" ]
  [ "$(line_of '^versions_view ')" -lt "$(line_of '^promote_maint ')" ]
  # Nothing is read inside the window: the final upload is not read back.
  [ "$(grep -c '^versions_view ' "$CALLS_LOG")" -eq 1 ]
  assert_step_called promote_final
}

@test "expected_runtime_date_differs_windowed: a mismatch aborts with nothing live" {
  run_wrapper 'DEPLOY_EXPECTED_RUNTIME={"compatibility_date": "2026-04-21", "compatibility_flags": []}'
  [ "$status" -ne 0 ]
  [[ "$output" == *"RUNTIME MISMATCH: uploaded version"*"compatibility_date 2025-01-01"*"is compatibility_date 2026-04-21"* ]]
  refute_step_called triggers
  refute_step_called promote_maint
  refute_step_called lock
  refute_step_called wrangler_rollback
}

@test "expected_runtime_flags_differ_windowed: a flag the upload lacks is a mismatch" {
  run_wrapper 'DEPLOY_EXPECTED_RUNTIME={"compatibility_date": "2025-01-01", "compatibility_flags": ["nodejs_compat"]}'
  [ "$status" -ne 0 ]
  [[ "$output" == *"RUNTIME MISMATCH"*"compatibility_flags [nodejs_compat]"* ]]
  refute_step_called triggers
  refute_step_called promote_maint
}

@test "expected_runtime_read_fails: a failed read says so and is not reported as a mismatch" {
  run_wrapper "DEPLOY_EXPECTED_RUNTIME=$MATCHING_RUNTIME" MOCK_FAIL_AT=versions_view
  [ "$status" -ne 0 ]
  [[ "$output" == *"RUNTIME NOT CHECKED: reading uploaded version"*"This is a failed read, not a runtime mismatch"* ]]
  [[ "$output" != *"RUNTIME MISMATCH"* ]]
  refute_step_called triggers
  refute_step_called promote_maint
}

@test "expected_runtime_read_blips: a transient read failure is retried and the deploy proceeds" {
  run_wrapper "DEPLOY_EXPECTED_RUNTIME=$MATCHING_RUNTIME" \
    DEPLOY_WRANGLER_RETRY_BACKOFF_SECONDS=0 MOCK_TRANSIENT_FAIL_AT=versions_view:1:fetch_failed
  [ "$status" -eq 0 ]
  [[ "$output" == *"versions_view: succeeded on attempt 2/2"* ]]
  assert_step_called promote_final
}

@test "expected_runtime_read_before_visible: the fresh upload's not-found is retried and the deploy proceeds" {
  # A read-back of the id the upload just returned has answered not-found; the
  # runtime check is the one read that knows its id came from that upload.
  run_wrapper "DEPLOY_EXPECTED_RUNTIME=$MATCHING_RUNTIME" \
    DEPLOY_WRANGLER_RETRY_BACKOFF_SECONDS=0 MOCK_TRANSIENT_FAIL_AT=versions_view:1:version_not_found
  [ "$status" -eq 0 ]
  [[ "$output" == *"versions_view: succeeded on attempt 2/2"* ]]
  assert_step_called promote_final
}

@test "expected_runtime_unreadable_document: a document stating no runtime says so and is not a mismatch" {
  # MOCK_VERSION_MESSAGE answers with annotations only: no resources at all.
  run_wrapper "DEPLOY_EXPECTED_RUNTIME=$MATCHING_RUNTIME" "MOCK_VERSION_MESSAGE=deploy-pipeline maintenance (smoketest)"
  [ "$status" -ne 0 ]
  [[ "$output" == *"RUNTIME NOT CHECKED: uploaded version"*"no resources.script_runtime"*"This is not a runtime mismatch"* ]]
  [[ "$output" != *"RUNTIME MISMATCH"* ]]
  refute_step_called triggers
}

@test "expected_runtime_differs_windowless: the final upload is refused before the promote" {
  run_wrapper DEPLOY_PIPELINE_MODE=windowless \
    'DEPLOY_EXPECTED_RUNTIME={"compatibility_date": "2026-04-21", "compatibility_flags": []}'
  [ "$status" -ne 0 ]
  [[ "$output" == *"RUNTIME MISMATCH"* ]]
  [ "$(line_of '^upload_final ')" -lt "$(line_of '^versions_view ')" ]
  refute_step_called promote_final
  refute_step_called wrangler_rollback
}

@test "expected_runtime_unparseable: a value that is not the document refuses before any call" {
  run_wrapper DEPLOY_EXPECTED_RUNTIME=
  [ "$status" -ne 0 ]
  [[ "$output" == *"DEPLOY_EXPECTED_RUNTIME is not JSON"* ]]
  refute_step_called upload_maint
}
