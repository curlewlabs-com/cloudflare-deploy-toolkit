#!/usr/bin/env bats
# End-to-end smoke test for the deploy pipeline, exercised through a minimal
# example wrapper.
#
# This test:
#   1. Verifies that the toolkit's pieces (deploy-lib, mock-wrangler,
#      example fixtures) wire together end-to-end. Without this test,
#      the toolkit ships with no in-repo demonstration that the
#      recovery layers actually fire.
#   2. Acts as a working example for external consumers writing their
#      first .bats driver. A real consumer's test driver looks almost
#      identical to this, just pointed at the consumer's own wrapper
#      and fixture directory.
#
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

# Helper: run the wrapper with arbitrary additional env vars.
# Usage: run_wrapper [MOCK_FAIL_AT=step]
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

# Point FIXTURE_DIR at a copy whose serving version id is a lone surrogate the
# document carries as its own JSON escape - the one kind no encoder accepts, so
# it can never become `wrangler rollback <id>`. ensure_ascii keeps the fixture
# ASCII on disk; json.loads turns the escape back into a surrogate when the
# pipeline reads it.
with_unusable_serving_version_id() {
  local fixtures="$BATS_TEST_TMPDIR/surrogate-id-fixtures"
  cp -R "$FIXTURE_DIR" "$fixtures"
  python3 - "$fixtures/deployments_list.json" <<'PYEOF'
import json
import pathlib
import sys

target = pathlib.Path(sys.argv[1])
document = json.loads(target.read_text())
document[-1]["versions"][0]["version_id"] = "\ud800-aaaa"
target.write_text(json.dumps(document, ensure_ascii=True))
PYEOF
  export FIXTURE_DIR="$fixtures"
}

refute_step_called() {
  if grep -q "^$1 " "$CALLS_LOG"; then
    echo "expected step '$1' to NOT be in call log, but it was:" >&2
    cat "$CALLS_LOG" >&2
    return 1
  fi
}

# A direct D1 command is logged even when the mock has no named step for it.
refute_database_command() {
  if grep -E ' d1 (execute|time-travel)' "$CALLS_LOG"; then
    return 1
  fi
}

@test "windowless_happy_path: no window taken, triggers before upload, migrate before promote" {
  run_wrapper
  [ "$status" -eq 0 ]
  assert_step_called deployments_list
  assert_step_called triggers
  # No expected build was given, so the serving version is not read.
  refute_step_called versions_view
  assert_step_called upload_final
  assert_step_called promote_final
  # Check the raw commands: a maintenance upload would classify as upload_final,
  # and a direct database command would otherwise appear as an unknown step.
  refute_database_command
  if grep -E 'MAINTENANCE_MODE|11111111-1111-1111-1111-111111111111@' "$CALLS_LOG"; then
    return 1
  fi
  refute_step_called wrangler_rollback
  # Ordering: trigger sync lands before the final upload (and therefore
  # before the promote) - the migrate hook runs between them, so a
  # config-coupled migrate side effect (e.g. a floor stamp) follows the
  # trigger push, matching the windowed body's relative order.
  triggers_line=$(grep -n "^triggers " "$CALLS_LOG" | head -n 1 | cut -d: -f1)
  upload_line=$(grep -n "^upload_final " "$CALLS_LOG" | head -n 1 | cut -d: -f1)
  promote_line=$(grep -n "^promote_final " "$CALLS_LOG" | head -n 1 | cut -d: -f1)
  [ "$triggers_line" -lt "$upload_line" ]
  [ "$upload_line" -lt "$promote_line" ]
}

@test "windowless_triggers_fail: aborts before any DB mutation or upload - old code serving, no recovery" {
  run_wrapper MOCK_FAIL_AT=triggers
  [ "$status" -ne 0 ]
  assert_step_called triggers
  refute_step_called upload_final
  refute_step_called promote_final
  refute_step_called wrangler_rollback
  refute_database_command
}

@test "windowless_migrate_fail: aborts fix-forward - no restore, no rollback, nothing promoted" {
  export MIGRATE_CMD="false"
  run_wrapper
  [ "$status" -ne 0 ]
  [[ "$output" == *"NO"*"Time-Travel restore runs on this path"* ]]
  assert_step_called triggers
  refute_step_called upload_final
  refute_step_called promote_final
  refute_step_called wrangler_rollback
  refute_database_command
}

@test "windowless_promote_final_fail: rolls the worker back even though no maint version was promoted" {
  run_wrapper MOCK_FAIL_AT=promote_final
  [ "$status" -ne 0 ]
  assert_step_called upload_final
  assert_step_called promote_final
  # A lost response can leave the new version live. Recover to the captured target.
  assert_step_called wrangler_rollback
  refute_database_command
}

@test "windowless_unusable_version_id: the windowless capture refuses it too" {
  # The windowless body records the same id as the rollback target and as the
  # TOCTOU reference, and its final-promote failure path is the one that needs
  # the worker rollback most - maint_live is never set there, so the recorded id
  # is the only thing standing between a promote that may have applied and a run
  # claiming the old version still serves. The windowed and windowless captures
  # go through one extraction path so neither body can record an id the rollback
  # cannot spend.
  with_unusable_serving_version_id

  run_wrapper
  [ "$status" -ne 0 ]
  assert_step_called deployments_list
  refute_step_called triggers
  refute_step_called upload_final
  refute_step_called promote_final
  refute_step_called wrangler_rollback
}

# The expected-build bind (--expected-serving-sha). The caller checked
# its delta against that build, so a serving version uploaded from any other -
# one a dashboard rollback brought back since - must stop the run before its
# first mutation, the trigger sync, rather than deploy over a state nobody
# checked. The fixture's serving version names the full commit below.
EXPECTED_SERVING_SHA=abc1234000000000000000000000000000000000

# Helper: run the wrapper expecting a serving commit, with env vars as
# run_wrapper takes them.
# Usage: run_wrapper_expecting <sha> [VAR=value ...]
run_wrapper_expecting() {
  local expected="$1"
  shift
  for env_kv in "$@"; do
    export "$env_kv"
  done
  run "$WRAPPER" staging --sha smoketest --expected-serving-sha "$expected"
}

@test "windowless_expected_build_matches: the bind reads the serving version and the deploy proceeds" {
  run_wrapper_expecting "$EXPECTED_SERVING_SHA"
  [ "$status" -eq 0 ]
  view_line=$(grep -n "^versions_view " "$CALLS_LOG" | head -n 1 | cut -d: -f1)
  triggers_line=$(grep -n "^triggers " "$CALLS_LOG" | head -n 1 | cut -d: -f1)
  [ "$view_line" -lt "$triggers_line" ]
  assert_step_called promote_final
}

@test "windowless_expected_build_differs: a different serving build aborts before any mutation" {
  run_wrapper_expecting def5678000000000000000000000000000000000
  [ "$status" -ne 0 ]
  [[ "$output" == *"uploaded from $EXPECTED_SERVING_SHA, not the expected def5678"* ]]
  assert_step_called versions_view
  refute_step_called triggers
  refute_step_called upload_final
  refute_step_called promote_final
  refute_step_called wrangler_rollback
}

@test "windowless_expected_build_not_final: a version no final upload wrote names no commit" {
  # A maintenance version, or one uploaded outside the pipeline, carries no
  # final-upload message, so which commit it serves is unknown.
  run_wrapper_expecting "$EXPECTED_SERVING_SHA" \
    "MOCK_VERSION_MESSAGE=deploy-pipeline maintenance (abc1234) commit=$EXPECTED_SERVING_SHA"
  [ "$status" -ne 0 ]
  [[ "$output" == *"uploaded from no commit its message names"* ]]
  refute_step_called triggers
  refute_step_called upload_final
}

@test "windowless_expected_build_short_only: a short SHA that prefixes the expected commit is not it" {
  # Distinct commits share short prefixes, so a version naming only the short
  # form - a rolled-back one uploaded before the full commit was written - must
  # not pass for the expected commit it happens to prefix.
  run_wrapper_expecting "$EXPECTED_SERVING_SHA" \
    "MOCK_VERSION_MESSAGE=deploy-pipeline final (abc1234) gen=1"
  [ "$status" -ne 0 ]
  [[ "$output" == *"uploaded from no commit its message names"* ]]
  refute_step_called triggers
  refute_step_called upload_final
}

@test "commit_sha_is_written: each upload's message names the full commit it was given" {
  # What the bind reads on a later deploy is what this deploy writes.
  export DEPLOY_COMMIT_SHA="$EXPECTED_SERVING_SHA"
  run "$WRAPPER" staging --sha abc1234
  [ "$status" -eq 0 ]
  grep -q "^upload_final .*--message deploy-pipeline final (abc1234) commit=$EXPECTED_SERVING_SHA" "$CALLS_LOG"
}

@test "windowless_expected_build_unreadable: a failed view aborts before any mutation" {
  run_wrapper_expecting "$EXPECTED_SERVING_SHA" \
    MOCK_FAIL_AT=versions_view
  [ "$status" -ne 0 ]
  refute_step_called triggers
  refute_step_called upload_final
  refute_step_called promote_final
}

@test "windowless_expected_build_not_json: a view that succeeds without a document says so and aborts" {
  # A zero-exit read can leave nothing on stderr, so the parse failure is the
  # only diagnostic the operator gets.
  local fixtures="$BATS_TEST_TMPDIR/not-json-fixtures"
  cp -R "$FIXTURE_DIR" "$fixtures"
  printf 'not a version document\n' > "$fixtures/versions_view.json"
  export FIXTURE_DIR="$fixtures"
  run_wrapper_expecting "$EXPECTED_SERVING_SHA"
  [ "$status" -ne 0 ]
  [[ "$output" == *"printed no JSON document"* ]]
  refute_step_called triggers
  refute_step_called upload_final
}

@test "windowless_postflight_fail: after a successful promote, fails loud with NO automatic recovery" {
  export POSTFLIGHT_CMD="false"
  run_wrapper
  [ "$status" -ne 0 ]
  [[ "$output" == *"Windowless postflight failed AFTER the final promotion"* ]]
  assert_step_called promote_final
  # The promote succeeded, so the attempted flag was cleared: a later failure
  # must not roll back a promote that landed - forward-fix only.
  refute_step_called wrangler_rollback
  refute_database_command
}

@test "unreadable history stops before mutation and requires verification" {
  fixtures="$BATS_TEST_TMPDIR/broken-history"
  cp -R "$FIXTURE_DIR" "$fixtures"
  printf '%s\n' 'not a deployment history' > "$fixtures/deployments_list.json"
  export FIXTURE_DIR="$fixtures"
  run_wrapper
  [ "$status" -ne 0 ]
  [[ "$output" == *"not valid JSON"* ]]
  [[ "$output" == *"after confirming it has never been deployed"* ]]
  refute_step_called triggers
  refute_step_called promote_final
}

@test "windowless_split_serving_refuses: a gradual split aborts before any mutation" {
  # A serving set that is not exactly one version at 100% must refuse the
  # windowless body outright - nothing uploaded, no triggers pushed.
  split_fixtures="$BATS_TEST_TMPDIR/split-fixtures"
  cp -R "$FIXTURE_DIR" "$split_fixtures"
  cat > "$split_fixtures/deployments_list.json" <<'EOF'
[
  {
    "id": "00000000-0000-0000-0000-cccccccccccc",
    "strategy": "percentage",
    "versions": [
      {"version_id": "99999999-9999-9999-9999-999999999999", "percentage": 50},
      {"version_id": "00000000-0000-0000-0000-000000000000", "percentage": 50}
    ],
    "created_on": "2025-01-03T00:00:00.000000Z"
  }
]
EOF
  export FIXTURE_DIR="$split_fixtures"
  run_wrapper
  [ "$status" -ne 0 ]
  [[ "$output" == *"expected exactly one version in the newest deployment, found 2"* ]]
  assert_step_called deployments_list
  refute_step_called triggers
  refute_step_called upload_final
}

@test "a preflight for a different serving version refuses before trigger or database changes" {
  export PREFLIGHT_CMD="sh -c 'echo checked=\$1/\$2; test \"\$2\" = 99999999-9999-9999-9999-999999999999' preflight"
  export MIGRATE_CMD="touch '$BATS_TEST_TMPDIR/migrated'"
  run_wrapper
  [ "$status" -ne 0 ]
  [[ "$output" == *"checked=staging/00000000-0000-0000-0000-000000000000"* ]]
  [ ! -e "$BATS_TEST_TMPDIR/migrated" ]
  assert_step_called deployments_list
  refute_step_called triggers
  refute_step_called upload_final
}

# A different deployment taking traffic after the hook validated the captured
# version must stop work at the next mutation boundary.
make_changed_deployment() {
  jq '.[-1].versions[0].version_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"'     "$FIXTURE_DIR/deployments_list.json" > "$BATS_TEST_TMPDIR/changed.json"
}

@test "a serving-version change during preflight stops trigger sync" {
  make_changed_deployment
  cp -R "$FIXTURE_DIR" "$BATS_TEST_TMPDIR/fixtures"
  export FIXTURE_DIR="$BATS_TEST_TMPDIR/fixtures"
  export PREFLIGHT_CMD="sh -c 'cp changed.json fixtures/deployments_list.json'"
  run_wrapper
  [ "$status" -ne 0 ]
  [[ "$output" == *"before updating triggers"* ]]
  refute_step_called triggers
}

@test "a serving-version change during trigger sync stops migration" {
  make_changed_deployment
  export MOCK_AFTER_TRIGGERS_DEPLOYMENTS="$BATS_TEST_TMPDIR/changed.json"
  export MIGRATE_CMD="sh -c 'touch migrated'"
  run_wrapper
  [ "$status" -ne 0 ]
  [[ "$output" == *"before the first DB mutation"* ]]
  assert_step_called triggers
  [ ! -e migrated ]
  refute_step_called upload_final
}
