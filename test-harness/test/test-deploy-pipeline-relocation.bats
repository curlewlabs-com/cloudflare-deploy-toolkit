#!/usr/bin/env bats
# The relocation body through the example wrapper and the deadline driver.
#
# A relocation's only durable record is the deploy locks of its source and its
# target, so the cases here are those states and the failure boundaries between
# them. What each one guards is the rule in deploy-lib/deploy_relocation.py: the
# source is reopened only by the run that fenced it, and only before that run
# reaches the promote. Past that nothing is rolled back and the source stays
# fenced: with the target locked too until the last step opens it, and with the
# relocation complete once it has.

SOURCE_DB="example-db-source"
TARGET_DB="example-db-staging"

setup() {
  TEST_DIR="$(cd "$(dirname "$BATS_TEST_FILENAME")" && pwd)"
  HARNESS_DIR="$(cd "$TEST_DIR/.." && pwd)"
  WRAPPER="$TEST_DIR/example-deploy-wrapper.sh"

  CALLS_LOG="$BATS_TEST_TMPDIR/calls.log"
  : > "$CALLS_LOG"

  export WRANGLER="$HARNESS_DIR/bin/mock-wrangler.sh"
  export FIXTURE_DIR="$TEST_DIR/example-fixtures"
  export MOCK_LOG_FILE="$CALLS_LOG"
  export MOCK_PER_DB_LOCK_STATE=1
  export DEPLOY_PIPELINE_MODE=relocation
  export DEPLOY_RELOCATION_SOURCE_DB_STAGING="$SOURCE_DB"
  export MIGRATE_CMD="true"
  export POSTFLIGHT_CMD="true"
  # Stands in for the consumer's copy: it leaves the target filled and locked,
  # which is the state the body then has to open.
  export RELOCATE_COPY_CMD="$TEST_DIR/example-hook.sh relocate-copy"
  export EXAMPLE_HOOK_SETS="relocate-copy=$CALLS_LOG.lock_state.$TARGET_DB=locked"
  # No retry waits: an unreadable lock is a case here, and its backoff is not.
  export DEPLOY_VERIFY_BACKOFF_SECONDS="0"
  # The drain is a wait with nothing in flight to wait for under the mock; its
  # place in the order is what relocation_drains_before_the_copy pins.
  export DEPLOY_DRAIN_SECONDS=0

  cd "$BATS_TEST_TMPDIR"
  cat > wrangler.toml <<'TOML'
name = "example"
main = "src/index.ts"
compatibility_date = "2025-01-01"
TOML
}

# Seed the source's and the target's deploy lock: locked, unlocked, or missing (no lock
# table, which is how a target that was never filled reads).
given_locks() {
  printf '%s\n' "$1" > "$CALLS_LOG.lock_state.$SOURCE_DB"
  printf '%s\n' "$2" > "$CALLS_LOG.lock_state.$TARGET_DB"
}

lock_of() {
  cat "$CALLS_LOG.lock_state.$1"
}

run_wrapper() {
  for env_kv in "$@"; do
    export "$env_kv"
  done
  run "$WRAPPER" staging --sha relocationtest
}

# A mutating step, against one named database. The lock READS also name a
# database, so the step name is what keeps them apart.
assert_mutated() {
  if ! grep -q "^$1 d1 execute $2 " "$CALLS_LOG"; then
    echo "expected '$1' against $2, call log:" >&2
    cat "$CALLS_LOG" >&2
    return 1
  fi
}

refute_mutated() {
  if grep -q "^$1 d1 execute $2 " "$CALLS_LOG"; then
    echo "expected NO '$1' against $2, call log:" >&2
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

line_of() {
  grep -n "$1" "$CALLS_LOG" | head -n 1 | cut -d: -f1
}

@test "relocation_happy_path: fence, copy, promote, then open the target - in that order" {
  given_locks unlocked missing
  run_wrapper
  [ "$status" -eq 0 ]
  assert_mutated lock "$SOURCE_DB"
  assert_mutated unlock "$TARGET_DB"
  # The source stays fenced for good: it is no longer what serves.
  refute_mutated unlock "$SOURCE_DB"
  refute_mutated lock "$TARGET_DB"
  refute_step_called upload_maint
  refute_step_called wrangler_rollback
  refute_step_called tt_restore
  [ "$(lock_of "$SOURCE_DB")" = "locked" ]
  [ "$(lock_of "$TARGET_DB")" = "unlocked" ]
  # Everything that can fail without a pause lands before the fence, and the
  # target opens only after the promote.
  [ "$(line_of '^triggers ')" -lt "$(line_of "^lock d1 execute $SOURCE_DB ")" ]
  [ "$(line_of '^upload_final ')" -lt "$(line_of "^lock d1 execute $SOURCE_DB ")" ]
  [ "$(line_of "^lock d1 execute $SOURCE_DB ")" -lt "$(line_of '^promote_final ')" ]
  [ "$(line_of '^promote_final ')" -lt "$(line_of "^unlock d1 execute $TARGET_DB ")" ]
}

# The fence refuses new requests, not the ones already past it. One admitted
# just before the lock flipped can still write to the source, and a write
# landing after the copy read the source is in neither the copy nor its proof,
# so the promote would leave it behind. The drain between the fence and the
# copy is what lets those requests finish first.
@test "relocation_drains_before_the_copy: admitted requests finish before the source is read" {
  given_locks unlocked missing
  run_wrapper
  [ "$status" -eq 0 ]
  before_drain="${output%%Drain in-flight requests*}"
  [ "$before_drain" != "$output" ]
  [[ "$before_drain" != *"Copy the source into the target"* ]]
  # The fence came first: the source's lock is in the call log, and its step
  # header in the output, ahead of the drain.
  assert_mutated lock "$SOURCE_DB"
  [[ "${output#*Drain in-flight requests}" == *"Copy the source into the target"* ]]
}

# A resumed run cannot know whether the run that fenced the source drained it:
# the lock records neither, and that run may have died the moment after the
# fence. So it drains before its copy too.
@test "relocation_resumed_run_drains_too: the fence alone says nothing about what is in flight" {
  given_locks locked locked
  run_wrapper DEPLOY_RELOCATION_RESUME=true
  [ "$status" -eq 0 ]
  before_drain="${output%%Drain in-flight requests*}"
  [ "$before_drain" != "$output" ]
  [[ "$before_drain" != *"Copy the source into the target"* ]]
}

@test "relocation_expected_runtime_differs: the upload is refused before the fence, so no write pauses" {
  # The runtime read happens outside the write pause, so neither a mismatch
  # nor a slow or failed read can spend the pause budget.
  given_locks unlocked missing
  run_wrapper 'DEPLOY_EXPECTED_RUNTIME={"compatibility_date": "2026-04-21", "compatibility_flags": []}'
  [ "$status" -ne 0 ]
  [[ "$output" == *"RUNTIME MISMATCH"* ]]
  [ "$(line_of '^upload_final ')" -lt "$(line_of '^versions_view ')" ]
  refute_mutated lock "$SOURCE_DB"
  refute_step_called promote_final
  [ "$(lock_of "$SOURCE_DB")" = "unlocked" ]
}

@test "relocation_copy_fails: the run that fenced the source reopens it, and the target stays shut" {
  given_locks unlocked missing
  run_wrapper RELOCATE_COPY_CMD=false
  [ "$status" -ne 0 ]
  assert_mutated lock "$SOURCE_DB"
  assert_mutated unlock "$SOURCE_DB"
  refute_mutated unlock "$TARGET_DB"
  refute_step_called promote_final
  refute_step_called wrangler_rollback
  [ "$(lock_of "$SOURCE_DB")" = "unlocked" ]
}

@test "relocation_fence_applies_but_reports_failure: the read-back decides, and the source is reopened" {
  # Cloudflare can apply a write and still report failure. A recovery that
  # trusted the exit status would leave the serving database fenced with no
  # relocation in flight.
  given_locks unlocked missing
  run_wrapper MOCK_FAIL_AT=lock MOCK_FAIL_SIDE_EFFECT_LOCK_STATE=locked
  [ "$status" -ne 0 ]
  assert_mutated unlock "$SOURCE_DB"
  refute_step_called promote_final
  [ "$(lock_of "$SOURCE_DB")" = "unlocked" ]
  # The copy hook never ran: the target is as it was.
  [ "$(lock_of "$TARGET_DB")" = "missing" ]
}

@test "relocation_copy_hook_leaves_the_target_open: refused before the promote, on a read and not an exit status" {
  # A target whose lock a hook cleared starts taking writes the moment the
  # promote applies - before the serving version is verified, and with recovery
  # still reasoning as though it were shut.
  given_locks unlocked missing
  run_wrapper "EXAMPLE_HOOK_SETS=relocate-copy=$CALLS_LOG.lock_state.$TARGET_DB=unlocked"
  [ "$status" -ne 0 ]
  refute_step_called promote_final
  refute_mutated unlock "$TARGET_DB"
  # This run fenced the source and never reached the promote, so it gives it back.
  assert_mutated unlock "$SOURCE_DB"
  [[ "$output" == *"Refusing to promote"* ]]
}

@test "relocation_migrate_hook_clears_the_target_lock: the read-back comes after every hook" {
  given_locks unlocked missing
  run_wrapper "MIGRATE_CMD=$TEST_DIR/example-hook.sh migrate" \
    "EXAMPLE_HOOK_SETS=relocate-copy=$CALLS_LOG.lock_state.$TARGET_DB=locked migrate=$CALLS_LOG.lock_state.$TARGET_DB=unlocked"
  [ "$status" -ne 0 ]
  refute_step_called promote_final
  assert_mutated unlock "$SOURCE_DB"
}

@test "relocation_source_lock_cleared_mid_fence: the copy no longer holds what the source does" {
  given_locks unlocked missing
  run_wrapper "EXAMPLE_HOOK_SETS=relocate-copy=$CALLS_LOG.lock_state.$TARGET_DB=locked relocate-copy=$CALLS_LOG.lock_state.$SOURCE_DB=unlocked"
  [ "$status" -ne 0 ]
  refute_step_called promote_final
  refute_mutated unlock "$TARGET_DB"
  [ "$(lock_of "$TARGET_DB")" = "locked" ]
}

# Expiry must follow fixture readiness: the deadline can otherwise prevent the
# hook from ever recording the processes whose cleanup these cases assert.
run_deadline_pipeline() {
  for env_kv in "$@"; do
    export "$env_kv"
  done
  if command -v python3.14 >/dev/null 2>&1; then
    run deadline_process_to_log python3.14 "$TEST_DIR/relocation_deadline.py"
  else
    run deadline_process_to_log python3 "$TEST_DIR/relocation_deadline.py"
  fi
  output="$(cat "$BATS_TEST_TMPDIR/deadline.out")"
  echo "$output" >&2
}

deadline_process_to_log() {
  # An orphaned hook must not hold bats' capture pipe open: the process
  # assertions need to run while the leaked processes still exist.
  "$@" > "$BATS_TEST_TMPDIR/deadline.out" 2>&1
}

assert_no_hook_process_is_left() {
  # Written as an `if`, because bats does not fail a test on a `! command` line.
  read -r hook_pid child_pid < "$BATS_TEST_TMPDIR/hang.pids"
  left=0
  for pid in "$hook_pid" "$child_pid"; do
    if kill -0 "$pid" 2>/dev/null; then
      echo "process $pid outlived the hook that was stopped" >&2
      # kill -0 also sees zombies; retain process state to diagnose CI failures.
      ps -o pid,ppid,pgid,stat,command -p "$pid" >&2 || true
      # Each survivor is cleaned up before the test fails, not only the first.
      kill -KILL "$pid" 2>/dev/null || true
      left=1
    fi
  done
  [ "$left" -eq 0 ]
}

assert_hook_and_its_child_are_gone() {
  assert_no_hook_process_is_left
  # A missed interception must not pass after waiting out the setup backstop.
  [[ "$output" == *"[deadline-fixture] hook ready; expiring wait"* ]]
  # The deadline's own message, so a run that ended some other way cannot pass.
  [[ "$output" == *"ran past the relocation's pause budget"* ]]
}

@test "relocation keeps both databases fenced when the promotion target is missing" {
  given_locks unlocked missing
  run_wrapper DEPLOY_WRANGLER_RETRY_BACKOFF_SECONDS=0 MOCK_TRANSIENT_FAIL_AT=promote_final:1:version_not_found_with_annotation
  [ "$status" -ne 0 ]
  [ "$(grep -c '^promote_final ' "$CALLS_LOG")" -eq 1 ]
  [ "$(lock_of "$SOURCE_DB")" = locked ]
  [ "$(lock_of "$TARGET_DB")" = locked ]
  refute_step_called wrangler_rollback
}

@test "relocation_copy_runs_past_the_pause_budget: stopped with everything it started, and the source is given back" {
  # A copy that hangs would otherwise hold the source's writes for as long as
  # it liked. Stopping only the hook's shell would leave the program it started
  # running against the databases after the source had been reopened.
  given_locks unlocked missing
  export EXAMPLE_HOOK_PID_FILE="$BATS_TEST_TMPDIR/hang.pids"
  run_deadline_pipeline "RELOCATE_COPY_CMD=$TEST_DIR/example-hook.sh hang"
  [ "$status" -ne 0 ]
  [[ "$output" == *"pause budget"* ]]
  assert_hook_and_its_child_are_gone
  refute_step_called promote_final
  assert_mutated unlock "$SOURCE_DB"
  [ "$(lock_of "$SOURCE_DB")" = "unlocked" ]
}

@test "relocation_stopped_hook_leaves_a_child_that_ignores_sigterm: the group is killed, not just asked" {
  # The hook's shell exits on SIGTERM at once. Deciding the stop was done by
  # watching the shell would leave this child running against the databases
  # after the source had been given back. Takes the stop sequence's whole grace
  # period, because that is what the child makes it wait out.
  given_locks unlocked missing
  export EXAMPLE_HOOK_PID_FILE="$BATS_TEST_TMPDIR/hang.pids"
  run_deadline_pipeline "RELOCATE_COPY_CMD=$TEST_DIR/example-hook.sh hang-ignoring-sigterm"
  [ "$status" -ne 0 ]
  assert_hook_and_its_child_are_gone
  assert_mutated unlock "$SOURCE_DB"
}

@test "relocation_pipeline_is_cancelled_mid_copy: the hook and what it started end with it" {
  # A fenced hook runs in a process group of its own, so a signal sent to the
  # pipeline's group no longer reaches it. Left alone it would outlive the
  # pipeline: a copy still running with nobody enforcing the pause budget and no
  # recovery to follow. The budget here is the default, far longer than this
  # test, so the deadline is not what ends the hook.
  given_locks unlocked missing
  export EXAMPLE_HOOK_PID_FILE="$BATS_TEST_TMPDIR/hang.pids"
  export RELOCATE_COPY_CMD="$TEST_DIR/example-hook.sh hang"
  "$WRAPPER" staging --sha relocationtest > "$BATS_TEST_TMPDIR/run.out" 2>&1 &
  pipeline_pid=$!
  # Until the hook is running. Bounded only so a hook that never starts fails
  # the test instead of hanging it; the bound is not what a healthy run races.
  waited=0
  until [ -s "$EXAMPLE_HOOK_PID_FILE" ]; do
    sleep 0.1
    waited=$((waited + 1))
    if [ "$waited" -gt 3000 ]; then
      echo "the copy hook never started:" >&2
      cat "$BATS_TEST_TMPDIR/run.out" >&2
      kill -KILL "$pipeline_pid" 2>/dev/null || true
      return 1
    fi
  done
  kill -TERM "$pipeline_pid"
  wait "$pipeline_pid" || true
  # Shown only when the test fails, which is when what the pipeline did matters.
  cat "$BATS_TEST_TMPDIR/run.out" >&2
  assert_no_hook_process_is_left
  # Cancelled, not recovered: the source stays as the fence left it, which is
  # the state a re-dispatch reads.
  [ "$(lock_of "$SOURCE_DB")" = "locked" ]
}

@test "relocation_cancelled_before_child_adoption: startup cannot orphan the copy" {
  # The driver delivers SIGTERM with a real child running but before Popen
  # returns its handle. This forces the ownership gap without racing the host.
  given_locks unlocked missing
  export EXAMPLE_HOOK_PID_FILE="$BATS_TEST_TMPDIR/hang.pids"
  run_deadline_pipeline "RELOCATE_COPY_CMD=$TEST_DIR/example-hook.sh hang" EXAMPLE_CANCEL_STARTUP=1
  [ "$status" -eq 143 ]
  [[ "$output" == *"[deadline-fixture] hook ready; cancelling startup"* ]]
  assert_no_hook_process_is_left
  refute_step_called promote_final
  [ "$(lock_of "$SOURCE_DB")" = "locked" ]
}

@test "relocation_migrate_runs_past_the_pause_budget: the deadline covers every fenced hook" {
  given_locks unlocked missing
  export EXAMPLE_HOOK_PID_FILE="$BATS_TEST_TMPDIR/hang.pids"
  run_deadline_pipeline "MIGRATE_CMD=$TEST_DIR/example-hook.sh hang"
  [ "$status" -ne 0 ]
  assert_hook_and_its_child_are_gone
  refute_step_called promote_final
  assert_mutated unlock "$SOURCE_DB"
}

@test "relocation_pause_spent_by_steps_that_are_not_hooks: not promoted, and the source is given back" {
  # The hooks are stopped at the deadline, which leaves the steps between them:
  # here every hook is instant and the serving read before the promote is slow.
  # Past the promote nothing is reopened, so this is the last point at which
  # running long can still be answered by giving the source back.
  given_locks unlocked missing
  run_wrapper DEPLOY_RELOCATION_MAX_PAUSE_SECONDS=1 MOCK_DELAY_AT=deployments_list:2
  [ "$status" -ne 0 ]
  [[ "$output" == *"pause budget"* ]]
  refute_step_called promote_final
  refute_mutated unlock "$TARGET_DB"
  assert_mutated unlock "$SOURCE_DB"
  [ "$(lock_of "$SOURCE_DB")" = "unlocked" ]
}

@test "relocation_resumed_run_past_the_pause_budget: stopped, and still never reopens the source" {
  given_locks locked missing
  export EXAMPLE_HOOK_PID_FILE="$BATS_TEST_TMPDIR/hang.pids"
  run_deadline_pipeline DEPLOY_RELOCATION_RESUME=true \
    "RELOCATE_COPY_CMD=$TEST_DIR/example-hook.sh hang"
  [ "$status" -ne 0 ]
  assert_hook_and_its_child_are_gone
  refute_mutated unlock "$SOURCE_DB"
  [ "$(lock_of "$SOURCE_DB")" = "locked" ]
}

@test "relocation_migrate_fails: still before the promote, so the source is reopened" {
  given_locks unlocked missing
  run_wrapper MIGRATE_CMD=false
  [ "$status" -ne 0 ]
  assert_mutated unlock "$SOURCE_DB"
  refute_mutated unlock "$TARGET_DB"
  refute_step_called promote_final
  [ "$(lock_of "$SOURCE_DB")" = "unlocked" ]
  # The copy landed, so the target holds a locked copy that is now stale.
  [ "$(lock_of "$TARGET_DB")" = "locked" ]
}

@test "relocation_promote_fails: source and target stay locked and nothing is rolled back" {
  # The promote can apply server-side and still report failure. Reopening the
  # source here could hand writes to a database the serving version no longer
  # reads, and a rollback would only add a state for the next run to reason
  # about: source and target locked refuses every write and loses none.
  given_locks unlocked missing
  run_wrapper MOCK_FAIL_AT=promote_final
  [ "$status" -ne 0 ]
  refute_mutated unlock "$SOURCE_DB"
  refute_mutated unlock "$TARGET_DB"
  refute_step_called wrangler_rollback
  [ "$(lock_of "$SOURCE_DB")" = "locked" ]
  [ "$(lock_of "$TARGET_DB")" = "locked" ]
  [[ "$output" == *"left FENCED on purpose"* ]]
  # The way forward needs the acknowledgement, so the instruction names it.
  [[ "$output" == *"DEPLOY_RELOCATION_RESUME=true"* ]]
  [[ "$output" != *"relocation is COMPLETE"* ]]
}

# The promote returned success and the serving set says otherwise. Opening the
# target then would record the relocation as complete while the Worker may still
# be the version bound to the fenced source, refusing every write.
assert_target_stays_shut_after_a_promote_that_did_not_take() {
  [ "$status" -ne 0 ]
  grep -q "^promote_final " "$CALLS_LOG"
  refute_mutated unlock "$TARGET_DB"
  refute_mutated unlock "$SOURCE_DB"
  refute_step_called wrangler_rollback
  [ "$(lock_of "$SOURCE_DB")" = "locked" ]
  [ "$(lock_of "$TARGET_DB")" = "locked" ]
  [[ "$output" == *"left FENCED on purpose"* ]]
  [[ "$output" == *"DEPLOY_RELOCATION_RESUME=true"* ]]
  [[ "$output" != *"relocation is COMPLETE"* ]]
}

@test "relocation_promote_returns_but_another_version_serves: the target is not opened on an exit status" {
  given_locks unlocked missing
  run_wrapper MOCK_SERVING_AFTER_PROMOTE=00000000-0000-0000-0000-000000000000
  assert_target_stays_shut_after_a_promote_that_did_not_take
}

@test "relocation_promote_returns_but_the_serving_set_is_split: a share of traffic is not the single version" {
  given_locks unlocked missing
  run_wrapper MOCK_SERVING_AFTER_PROMOTE=split
  assert_target_stays_shut_after_a_promote_that_did_not_take
}

@test "relocation_postflight_fails: after a promote that SUCCEEDED the source still stays fenced" {
  # final_promote_attempted is cleared by a successful promote, so this is the
  # case a recovery keyed on it would get wrong: the target is open and
  # serving, and reopening the source would leave the source open beside it.
  given_locks unlocked missing
  run_wrapper POSTFLIGHT_CMD=false
  [ "$status" -ne 0 ]
  refute_mutated unlock "$SOURCE_DB"
  refute_step_called wrangler_rollback
  [ "$(lock_of "$SOURCE_DB")" = "locked" ]
  [ "$(lock_of "$TARGET_DB")" = "unlocked" ]
  # The target is open and taking writes. "Locked, re-dispatch" is the opposite
  # instruction, and re-dispatching would be refused by the open target anyway.
  [[ "$output" == *"relocation is COMPLETE"* ]]
  [[ "$output" != *"left FENCED on purpose"* ]]
}

@test "relocation_resumes_after_a_dead_run: a fenced source and a landed copy go forward" {
  given_locks locked locked
  run_wrapper DEPLOY_RELOCATION_RESUME=true
  [ "$status" -eq 0 ]
  # Already fenced, so there is nothing to lock.
  refute_mutated lock "$SOURCE_DB"
  assert_mutated unlock "$TARGET_DB"
  [ "$(lock_of "$SOURCE_DB")" = "locked" ]
  [ "$(lock_of "$TARGET_DB")" = "unlocked" ]
}

@test "relocation_resumed_run_fails: it did not fence the source, so it never reopens it" {
  # An earlier run fenced the source, and whether ITS promote applied is not
  # knowable from here. A failure before this run's own promote must not be
  # read as permission to reopen.
  given_locks locked missing
  run_wrapper DEPLOY_RELOCATION_RESUME=true RELOCATE_COPY_CMD=false
  [ "$status" -ne 0 ]
  refute_mutated unlock "$SOURCE_DB"
  refute_mutated unlock "$TARGET_DB"
  [ "$(lock_of "$SOURCE_DB")" = "locked" ]
}

@test "relocation_refuses_a_locked_source_nobody_vouched_for: the lock cannot say who set it" {
  # A deploy that died inside its maintenance window leaves this same bit, over
  # a database that may be part-way through a migration. Continuing from it is
  # a person's call, made at dispatch.
  given_locks locked missing
  run_wrapper
  [ "$status" -ne 0 ]
  [[ "$output" == *"cannot say who set it"* ]]
  refute_step_called upload_final
  refute_step_called lock
  refute_step_called unlock
}

@test "relocation_refuses_a_resume_with_nothing_fenced: the dispatch assumed a state that is not there" {
  given_locks unlocked missing
  run_wrapper DEPLOY_RELOCATION_RESUME=true
  [ "$status" -ne 0 ]
  [[ "$output" == *"not fenced"* ]]
  refute_step_called upload_final
  refute_step_called lock
}

@test "relocation_source_locked_by_another_actor_mid_run: fences nothing and reopens nothing" {
  # Locking over another holder reads back as locked, and the run would then
  # take the fence for its own and reopen it on failure - clearing a lock that
  # belongs to a deploy still inside its window.
  given_locks unlocked missing
  run_wrapper "MOCK_LOCK_STATE_AT=upload_final:$SOURCE_DB:locked"
  [ "$status" -ne 0 ]
  refute_mutated lock "$SOURCE_DB"
  refute_mutated unlock "$SOURCE_DB"
  [ "$(lock_of "$SOURCE_DB")" = "locked" ]
  [ "$(lock_of "$TARGET_DB")" = "missing" ]
}

@test "relocation_refuses_an_open_target: complete, or never a relocation's target" {
  given_locks locked unlocked
  run_wrapper DEPLOY_RELOCATION_RESUME=true
  [ "$status" -ne 0 ]
  [[ "$output" == *"already open"* ]]
  refute_step_called upload_final
  refute_step_called lock
  refute_step_called unlock
}

@test "relocation_refuses_a_stale_copy: a locked target under an open source is never overwritten" {
  given_locks unlocked locked
  run_wrapper
  [ "$status" -ne 0 ]
  [[ "$output" == *"empty the target"* ]]
  refute_step_called upload_final
  refute_step_called lock
  refute_step_called unlock
}

@test "relocation_config_is_refused_before_anything_runs: no source named" {
  given_locks unlocked missing
  unset DEPLOY_RELOCATION_SOURCE_DB_STAGING
  run_wrapper
  [ "$status" -ne 0 ]
  [[ "$output" == *"DEPLOY_RELOCATION_SOURCE_DB_STAGING is not set"* ]]
  [ ! -s "$CALLS_LOG" ]
}
