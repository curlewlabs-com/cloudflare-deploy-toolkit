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
# The test cases below cover:
#   - happy_path:       full sequence runs without recovery
#   - fail_triggers:    cron-limit-shaped failure aborts BEFORE the
#                       point-of-no-return - no rollback needed.
#                       (regression guard for the 2026-04-08 incident)
#   - fail_lock:        failure AFTER promote (maint_live=true) but
#                       BEFORE save_bookmark - triggers worker rollback
#                       only (Layer 2)
#   - fail_postflight:  failure AFTER save_bookmark - triggers full
#                       3-layer recovery (D1 restore + worker rollback
#                       + unlock) (Layer 3)

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

# ----------------------------------------------------------------------
# Happy path: every step runs in order, no recovery actions.
# ----------------------------------------------------------------------
@test "happy_path: full sequence with no recovery" {
  run_wrapper
  [ "$status" -eq 0 ]
  assert_step_called deployments_list
  assert_step_called upload_maint
  assert_step_called triggers
  assert_step_called promote_maint
  assert_step_called lock
  assert_step_called bookmark
  assert_step_called upload_final
  assert_step_called promote_final
  assert_step_called unlock
  refute_step_called wrangler_rollback
  refute_step_called tt_restore
}

# ----------------------------------------------------------------------
# Layer 1: failure at the triggers step. This is the 2026-04-08
# incident shape - failure happens BEFORE the maintenance version is
# promoted, so the worker is still serving old code and no recovery
# is needed.
# ----------------------------------------------------------------------
@test "fail_triggers: regression guard for 2026-04-08 incident" {
  run_wrapper MOCK_FAIL_AT=triggers
  [ "$status" -ne 0 ]
  assert_step_called upload_maint
  assert_step_called triggers
  refute_step_called promote_maint
  refute_step_called wrangler_rollback
  refute_step_called tt_restore
}

# ----------------------------------------------------------------------
# Layer 2: failure at the lock step. The maintenance version was
# successfully promoted (maint_live=true) but the DB lock attempt
# failed and read-back stayed unlocked, so no bookmark exists yet. Recovery is a
# worker rollback to the previous version - no D1 restore is needed
# because no migration ran.
# ----------------------------------------------------------------------
@test "fail_lock: triggers worker rollback only (Layer 2)" {
  run_wrapper MOCK_FAIL_AT=lock
  [ "$status" -ne 0 ]
  assert_step_called promote_maint
  assert_step_called lock
  assert_step_called wrangler_rollback
  refute_step_called tt_restore
  refute_step_called bookmark
}

# ----------------------------------------------------------------------
# A failed maintenance promote still rolls the worker back, because the
# call can apply server-side and report failure anyway - the same
# split-brain the final promote already guards. maint_live is set only
# AFTER the call returns, so without the in-flight flag the recovery
# path would exit clean on a promote that landed, leaving the worker
# serving maintenance mode (a 503 for every request) while the run
# reports "old version still serving". Cloudflare's gradual-deployment
# model being atomic does not answer this: atomicity decides whether the
# promotion happened, not whether the client learned that it did.
#
# The rollback is idempotent, so the cost when the promote genuinely did
# not apply is one call that re-pins the version already serving.
# ----------------------------------------------------------------------
@test "fail_promote_maint: rolls the worker back because the promote may have applied" {
  run_wrapper MOCK_FAIL_AT=promote_maint
  [ "$status" -ne 0 ]
  assert_step_called promote_maint
  assert_step_called wrangler_rollback
  # No lock was taken and no bookmark saved, so neither the unlock nor the D1
  # restore has anything to do.
  refute_step_called tt_restore
  refute_step_called unlock
}

# ----------------------------------------------------------------------
# A stale pre-existing deploy lock must abort before any remote mutation.
# This guards the new precondition check: don't upload or promote
# anything if the database already says another deploy still owns the lock.
# ----------------------------------------------------------------------
@test "preexisting_lock: aborts before upload" {
  run_wrapper MOCK_INITIAL_LOCK_STATE=locked
  [ "$status" -ne 0 ]
  assert_step_called lock_state
  refute_step_called upload_maint
  refute_step_called promote_maint
}

# ----------------------------------------------------------------------
# Cloudflare can fail the lock command after the write already applied.
# In that case the pipeline must trust read-back, rollback the worker, and
# still attempt unlock on the recovery path.
# ----------------------------------------------------------------------
@test "fail_lock_with_side_effect: rollback plus unlock when read-back says locked" {
  run_wrapper MOCK_FAIL_AT=lock MOCK_FAIL_SIDE_EFFECT_LOCK_STATE=locked
  [ "$status" -ne 0 ]
  assert_step_called lock
  assert_step_called lock_state
  assert_step_called wrangler_rollback
  assert_step_called unlock
  refute_step_called tt_restore
}

# ----------------------------------------------------------------------
# Layer 3: failure at postflight (after lock+bookmark). Recovery is
# the full 3-layer dance - D1 Time Travel restore, then worker
# rollback, then unlock.
# ----------------------------------------------------------------------
@test "fail_postflight: triggers full 3-layer recovery" {
  export POSTFLIGHT_CMD="false"
  run_wrapper
  [ "$status" -ne 0 ]
  assert_step_called bookmark
  assert_step_called tt_restore
  assert_step_called wrangler_rollback
  assert_step_called unlock
}

# ----------------------------------------------------------------------
# The other kind of surrogate, and the one that has to refuse. A byte that is
# not UTF-8 round-trips to wrangler through argv, so it is rendered and the
# deploy continues (the case below). A lone surrogate the document carries as
# its own JSON escape does not: no encoder takes it, so it cannot become
# `wrangler rollback <id>`.
#
# Left to run, it reaches the recovery path and dies there. The worker rollback
# is the one recovery action deploy_recovery does not wrap, so the exception
# escapes: the call log ends at tt_restore, and the rollback and the unlock
# never run - the maintenance version keeps serving and the lock stays set.
# Refusing at the capture happens before any upload, promote, or lock, so the
# refused deploy leaves nothing behind to recover.
# ----------------------------------------------------------------------
@test "unusable_version_id: an id no encoder accepts refuses before anything goes live" {
  with_unusable_serving_version_id

  run_wrapper
  [ "$status" -ne 0 ]
  assert_step_called deployments_list
  # Nothing was uploaded, promoted, or locked, so there is nothing to unwind.
  refute_step_called upload_maint
  refute_step_called promote_maint
  refute_step_called lock
  refute_step_called tt_restore
  refute_step_called wrangler_rollback
}

# ----------------------------------------------------------------------
# The API can render a byte that is not UTF-8, and wrangler_runner decodes its
# streams with surrogateescape on purpose so the transient classifier sees the
# text Cloudflare actually sent. That byte then reaches the values the pipeline
# prints. Python's stdout is `strict` by default, so printing one used to raise
# UnicodeEncodeError - and the bookmark is printed AFTER the promote and AFTER
# the lock, which put the abort past the point of no return: a healthy deploy
# was rolled back because a log line could not be encoded.
#
# The byte is injected here rather than committed: a fixture carrying it would
# not be the ASCII the tracked tree requires.
# ----------------------------------------------------------------------
@test "unencodable_bookmark: a non-UTF-8 byte in wrangler output does not abort the deploy" {
  fixtures="$BATS_TEST_TMPDIR/raw-byte-fixtures"
  cp -R "$FIXTURE_DIR" "$fixtures"
  python3 - "$fixtures/bookmark.json" <<'PYEOF'
import pathlib
import sys

target = pathlib.Path(sys.argv[1])
raw = target.read_bytes()
replaced = raw.replace(b'"bookmark":"0000', b'"bookmark":"00\xff0')
if replaced == raw:
    raise SystemExit("fixture shape changed - the bookmark no longer starts 0000")
target.write_bytes(replaced)
PYEOF
  export FIXTURE_DIR="$fixtures"
  # A real deploy workflow sets this, and the bundle is the
  # other place every one of these values is written - on the way out, after the
  # recovery has finished, so a raise there turns a completed run into a
  # traceback.
  export DEPLOY_DIAGNOSTICS_DIR="$BATS_TEST_TMPDIR/diagnostics"

  run_wrapper
  # The whole sequence, not a recovery: the byte is a rendering problem, and the
  # deploy it was aborting had nothing wrong with it.
  [ "$status" -eq 0 ]
  # The bundle landed rather than raising, and carries the byte in escaped form.
  [ -f "$DEPLOY_DIAGNOSTICS_DIR/deploy-diagnostics.txt" ]
  grep -q '^bookmark=' "$DEPLOY_DIAGNOSTICS_DIR/deploy-diagnostics.txt"
  assert_step_called bookmark
  assert_step_called upload_final
  assert_step_called promote_final
  assert_step_called unlock
  refute_step_called wrangler_rollback
  refute_step_called tt_restore
  # Rendered, not dropped: the operator still gets the line, with the byte in
  # its escaped form.
  [[ "$output" == *"Bookmark: "* ]]
}

# ----------------------------------------------------------------------
# The step summary is operator-facing logging, so an unwritable one must not
# take the recovery path with it. The unguarded write this replaced aborted the
# run wherever it failed, the recovery path included, and stopped the deploy
# mid-window with the maintenance version serving and the lock still set - so
# the next deploy refused to start on a lock it could not prove clear.
#
# The migrate hook replaces the summary FILE WITH A DIRECTORY rather than
# chmod-ing it, so the write fails with EISDIR for any uid: a root runner
# writes straight through a mode of 000 and would pass this case having
# exercised nothing.
# ----------------------------------------------------------------------
@test "broken_step_summary: an unwritable summary does not stop the 3-layer recovery" {
  breaker="$BATS_TEST_TMPDIR/break-summary.sh"
  cat > "$breaker" <<'EOF'
#!/bin/sh
set -eu
rm -f "$GITHUB_STEP_SUMMARY"
mkdir "$GITHUB_STEP_SUMMARY"
EOF
  chmod +x "$breaker"
  # The pipeline keys the summary off this variable alone, never off
  # GITHUB_ACTIONS, so the run needs nothing else to have a summary to break.
  export GITHUB_STEP_SUMMARY="$BATS_TEST_TMPDIR/step-summary.md"
  : > "$GITHUB_STEP_SUMMARY"
  export MIGRATE_CMD="$breaker"
  export POSTFLIGHT_CMD="false"

  run_wrapper
  [ "$status" -ne 0 ]
  # The same recovery layers fail_postflight asserts, now with every summary
  # write after the migrate hook failing.
  assert_step_called bookmark
  assert_step_called tt_restore
  assert_step_called wrangler_rollback
  assert_step_called unlock
  # Reported once, not once per line: the recovery path alone writes a dozen
  # more rows after the file is gone, and repeating the same message for each
  # would bury the recovery output the operator is actually reading.
  # `|| reports=0`, because grep -c exits 1 on no match and a bare assignment
  # would abort the test there instead of at the assertion that names the count.
  reports=$(printf '%s\n' "$output" | grep -c 'step summary write failed') || reports=0
  [ "$reports" -eq 1 ]
}

# ----------------------------------------------------------------------
# The unlock is an obligation, not a recovery layer, and it is discharged on
# every other failure shape - a lock left set refuses the next deploy. This is
# one of the states where leaving it set is the safer answer;
# restore_fails_after_migrate below is the other.
#
# The final promote can apply server-side and still report failure, so after one
# the recovery path tries to put the previous version back. When that rollback
# also fails, real code from the failed promote may be serving while the D1
# restore has already taken the schema back to before the migration. Unlocking
# there admits writes from new code against an old schema; the lock is the last
# thing stopping them, and the next deploy refusing to start on it is the point.
#
# Contrast fail_postflight above, which unlocks: there the rollback succeeds, so
# the serving version is known again.
# ----------------------------------------------------------------------
@test "rollback_fails_after_promote: the unlock is withheld, not attempted" {
  # MOCK_FAIL_AT takes a list for this case: it fails the promote that puts the
  # new version live, and the rollback that would take it back down.
  run_wrapper MOCK_FAIL_AT="promote_final wrangler_rollback"
  [ "$status" -ne 0 ]
  assert_step_called bookmark
  assert_step_called promote_final
  assert_step_called wrangler_rollback
  # The whole point: no unlock call, and no unlock read-back either.
  refute_step_called unlock
  [[ "$output" == *"Database unlock WITHHELD"* ]]
  [[ "$output" == *"lock is left SET on purpose"* ]]
}

@test "rollback_succeeds_after_promote: the unlock still runs" {
  # The other side of the same branch, so the withholding above cannot be
  # mistaken for "a failed promote stops unlocking". The rollback succeeding is
  # what makes the serving version known, which is what re-permits the unlock.
  run_wrapper MOCK_FAIL_AT=promote_final
  [ "$status" -ne 0 ]
  assert_step_called promote_final
  assert_step_called wrangler_rollback
  assert_step_called unlock
  [[ "$output" != *"WITHHELD"* ]]
}

# ----------------------------------------------------------------------
# The other state where the lock has to stay set: the D1 restore failed. The
# migrate ran before the final promote and failed part-way, the rollback put
# the previous version back, and the restore that should have taken the schema
# back to the bookmark did not. Unlocking there lets the old code write into a
# half-migrated database, which nothing afterwards can untangle; a locked
# database is recovered through the unlock workflow once someone has looked.
# ----------------------------------------------------------------------
@test "restore_fails_after_migrate: the unlock is withheld, not attempted" {
  export MIGRATE_CMD="false"
  run_wrapper MOCK_FAIL_AT=tt_restore
  [ "$status" -ne 0 ]
  assert_step_called bookmark
  assert_step_called tt_restore
  assert_step_called wrangler_rollback
  refute_step_called promote_final
  refute_step_called unlock
  [[ "$output" == *"Database unlock WITHHELD"* ]]
  [[ "$output" == *"D1 restore failed"* ]]
}

# ----------------------------------------------------------------------
# Windowless body (DEPLOY_PIPELINE_MODE=windowless): no maintenance
# version, no lock, no drain, no bookmark, no restore. Selection is
# explicit config - the default (every case above) stays windowed.
# ----------------------------------------------------------------------
@test "windowless_happy_path: no window taken, triggers before upload, migrate before promote" {
  run_wrapper DEPLOY_PIPELINE_MODE=windowless
  [ "$status" -eq 0 ]
  assert_step_called deployments_list
  assert_step_called triggers
  # No expected build was given, so the serving version is not read.
  refute_step_called versions_view
  assert_step_called upload_final
  assert_step_called promote_final
  refute_step_called upload_maint
  refute_step_called promote_maint
  refute_step_called lock
  refute_step_called bookmark
  refute_step_called unlock
  refute_step_called tt_restore
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
  run_wrapper DEPLOY_PIPELINE_MODE=windowless MOCK_FAIL_AT=triggers
  [ "$status" -ne 0 ]
  assert_step_called triggers
  refute_step_called upload_final
  refute_step_called promote_final
  refute_step_called upload_maint
  refute_step_called lock
  refute_step_called bookmark
  refute_step_called wrangler_rollback
  refute_step_called tt_restore
  refute_step_called unlock
}

@test "windowless_migrate_fail: aborts fix-forward - no restore, no rollback, nothing promoted" {
  export MIGRATE_CMD="false"
  run_wrapper DEPLOY_PIPELINE_MODE=windowless
  [ "$status" -ne 0 ]
  [[ "$output" == *"NO"*"Time-Travel restore runs on this path"* ]]
  assert_step_called triggers
  refute_step_called upload_final
  refute_step_called promote_final
  refute_step_called wrangler_rollback
  refute_step_called tt_restore
  refute_step_called lock
  refute_step_called bookmark
  refute_step_called unlock
}

@test "windowless_promote_final_fail: rolls the worker back even though no maint version was promoted" {
  run_wrapper DEPLOY_PIPELINE_MODE=windowless MOCK_FAIL_AT=promote_final
  [ "$status" -ne 0 ]
  assert_step_called upload_final
  assert_step_called promote_final
  # The final promote can apply server-side yet still report failure. Because
  # maint_live never goes true on this body, the split-brain would otherwise
  # leave the new (maybe-live) version up while the run claims "old version
  # serving". The final_promote_attempted flag makes the recovery path roll
  # the worker back to the recorded serving version - a worker rollback only
  # (no bookmark, so no D1 restore / unlock).
  assert_step_called wrangler_rollback
  refute_step_called tt_restore
  refute_step_called unlock
  refute_step_called bookmark
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

  run_wrapper DEPLOY_PIPELINE_MODE=windowless
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
  run_wrapper_expecting "$EXPECTED_SERVING_SHA" DEPLOY_PIPELINE_MODE=windowless
  [ "$status" -eq 0 ]
  view_line=$(grep -n "^versions_view " "$CALLS_LOG" | head -n 1 | cut -d: -f1)
  triggers_line=$(grep -n "^triggers " "$CALLS_LOG" | head -n 1 | cut -d: -f1)
  [ "$view_line" -lt "$triggers_line" ]
  assert_step_called promote_final
}

@test "windowless_expected_build_differs: a different serving build aborts before any mutation" {
  run_wrapper_expecting def5678000000000000000000000000000000000 DEPLOY_PIPELINE_MODE=windowless
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
  run_wrapper_expecting "$EXPECTED_SERVING_SHA" DEPLOY_PIPELINE_MODE=windowless \
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
  run_wrapper_expecting "$EXPECTED_SERVING_SHA" DEPLOY_PIPELINE_MODE=windowless \
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
  grep -q "^upload_maint .*--message deploy-pipeline maintenance (abc1234) commit=$EXPECTED_SERVING_SHA" "$CALLS_LOG"
  grep -q "^upload_final .*--message deploy-pipeline final (abc1234) commit=$EXPECTED_SERVING_SHA" "$CALLS_LOG"
}

@test "windowless_expected_build_unreadable: a failed view aborts before any mutation" {
  run_wrapper_expecting "$EXPECTED_SERVING_SHA" DEPLOY_PIPELINE_MODE=windowless \
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
  run_wrapper_expecting "$EXPECTED_SERVING_SHA" DEPLOY_PIPELINE_MODE=windowless
  [ "$status" -ne 0 ]
  [[ "$output" == *"printed no JSON document"* ]]
  refute_step_called triggers
  refute_step_called upload_final
}

@test "windowed_expected_build_differs: the windowed capture binds too, before the maintenance upload" {
  run_wrapper_expecting def5678000000000000000000000000000000000
  [ "$status" -ne 0 ]
  assert_step_called versions_view
  refute_step_called upload_maint
  refute_step_called triggers
  refute_step_called promote_maint
  refute_step_called lock
}

@test "windowless_postflight_fail: after a successful promote, fails loud with NO automatic recovery" {
  export POSTFLIGHT_CMD="false"
  run_wrapper DEPLOY_PIPELINE_MODE=windowless
  [ "$status" -ne 0 ]
  [[ "$output" == *"Windowless postflight failed AFTER the final promotion"* ]]
  assert_step_called promote_final
  # The promote succeeded, so the attempted flag was cleared: a later failure
  # must not roll back a promote that landed - forward-fix only.
  refute_step_called wrangler_rollback
  refute_step_called tt_restore
  refute_step_called unlock
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
  run_wrapper DEPLOY_PIPELINE_MODE=windowless
  [ "$status" -ne 0 ]
  [[ "$output" == *"not a single version at 100%"* ]]
  assert_step_called deployments_list
  refute_step_called triggers
  refute_step_called upload_final
  refute_step_called upload_maint
  refute_step_called lock
}

@test "windowed_split_serving_refuses: a gradual split aborts before the window opens" {
  # The windowed body captures a single rollback target and, on failure after
  # the promote, rolls the worker back to it. Under a split, reading the
  # first-listed version would make that target arbitrary - and a consumer
  # preflight that samples the serving state once (a rollback floor
  # that reads one /health response, say) cannot see the other slice at all. Refuse
  # before the maintenance version is uploaded or the lock taken.
  split_fixtures="$BATS_TEST_TMPDIR/windowed-split-fixtures"
  cp -R "$FIXTURE_DIR" "$split_fixtures"
  cat > "$split_fixtures/deployments_list.json" <<'EOF'
[
  {
    "id": "00000000-0000-0000-0000-dddddddddddd",
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
  [[ "$output" == *"not a single version at 100%"* ]]
  assert_step_called deployments_list
  refute_step_called upload_maint
  refute_step_called triggers
  refute_step_called promote_maint
  refute_step_called lock
  refute_step_called upload_final
}

@test "windowed_bootstrap_still_captures_no_target: an empty deployments list is not a split" {
  # The split refusal above must not swallow the genuine bootstrap shape: a
  # readable list with no deployment to capture is what staging's first-ever
  # deploy sees, and it continues with no rollback target.
  empty_fixtures="$BATS_TEST_TMPDIR/empty-fixtures"
  cp -R "$FIXTURE_DIR" "$empty_fixtures"
  printf '[]\n' > "$empty_fixtures/deployments_list.json"
  export FIXTURE_DIR="$empty_fixtures"
  run_wrapper
  [ "$status" -eq 0 ]
  [[ "$output" == *"Previous version: <none - first deploy>"* ]]
  assert_step_called upload_maint
}

@test "windowed_unreadable_versions_is_not_bootstrap: a null versions array refuses" {
  # "Nothing is serving yet" and "the serving set could not be read" look alike
  # at the capture and must not share a branch: only an empty array of
  # deployments, or an empty array of versions, is a bootstrap. A null (or
  # absent, or non-array) `versions` says the set is unreadable, and continuing
  # there would hand staging a deploy with no rollback target on evidence
  # nobody checked.
  null_fixtures="$BATS_TEST_TMPDIR/null-versions-fixtures"
  cp -R "$FIXTURE_DIR" "$null_fixtures"
  printf '[{"id":"dep-1","versions":null,"created_on":"2025-01-03T00:00:00.000000Z"}]\n' \
    > "$null_fixtures/deployments_list.json"
  export FIXTURE_DIR="$null_fixtures"
  run_wrapper
  [ "$status" -ne 0 ]
  refute_step_called upload_maint
  refute_step_called lock
}

@test "unknown_mode_aborts: an unrecognized DEPLOY_PIPELINE_MODE runs nothing" {
  run_wrapper DEPLOY_PIPELINE_MODE=windowlss
  [ "$status" -ne 0 ]
  [[ "$output" == *"unrecognized DEPLOY_PIPELINE_MODE"* ]]
  [ ! -s "$CALLS_LOG" ]
}

