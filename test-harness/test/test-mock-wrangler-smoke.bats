#!/usr/bin/env bats
# Smoke test for mock-wrangler.sh: spin up a tempdir with a single fixture file
# and verify the mock can read it back. This is a sanity check that the mock's
# classification and fixture-loading wiring works end-to-end. Per-step content
# and failure injection are exercised by the deploy-pipeline suites beside this
# one and by a consumer's own deploy-script test.

setup() {
  TEST_DIR="$(cd "$(dirname "$BATS_TEST_FILENAME")" && pwd)"
  HARNESS_DIR="$(cd "$TEST_DIR/.." && pwd)"
}

@test "mock-wrangler classifies a call, reads its fixture and logs the step" {
  mkdir -p "$BATS_TEST_TMPDIR/fixtures"
  echo "smoke-test fixture content" >"$BATS_TEST_TMPDIR/fixtures/upload_final.txt"
  SMOKE_LOG="$BATS_TEST_TMPDIR/calls.log"
  : >"$SMOKE_LOG"
  output=$(MOCK_LOG_FILE="$SMOKE_LOG" \
    FIXTURE_DIR="$BATS_TEST_TMPDIR/fixtures" \
    "$HARNESS_DIR/bin/mock-wrangler.sh" versions upload --env staging)
  if [ "$output" != "smoke-test fixture content" ]; then
    echo "  [FAIL] mock returned unexpected output: $output" >&2
    return 1
  fi
  if ! grep -q "^upload_final " "$SMOKE_LOG"; then
    echo "  [FAIL] mock did not log the upload_final step" >&2
    cat "$SMOKE_LOG" >&2
    return 1
  fi
}

@test "unexpected promotion arguments are recorded and cannot move traffic" {
  export FIXTURE_DIR="$BATS_TEST_TMPDIR" MOCK_LOG_FILE="$BATS_TEST_TMPDIR/calls.log"
  final=22222222-2222-2222-2222-222222222222
  previous=99999999-9999-9999-9999-999999999999
  for extra in "$previous@50%" "$previous" "--dry-run"; do
    run "$HARNESS_DIR/bin/mock-wrangler.sh" versions deploy "$final@100%" \
      --env staging --yes --message "a deployment" "$extra"
    [ "$status" -ne 0 ]
    grep -Fq "promote_final versions deploy $final@100% --env staging --yes --message a deployment $extra" "$MOCK_LOG_FILE"
    [ ! -e "$MOCK_LOG_FILE.promoted" ]
  done
}

@test "human-readable deployment history cannot satisfy a machine read" {
  export FIXTURE_DIR="$BATS_TEST_TMPDIR" MOCK_LOG_FILE="$BATS_TEST_TMPDIR/calls.log"
  printf '[]\n' > "$FIXTURE_DIR/deployments_list.json"
  run "$HARNESS_DIR/bin/mock-wrangler.sh" deployments list --env staging
  [ "$status" -ne 0 ]
  [[ "$output" == *"requires --json"* ]]
}
