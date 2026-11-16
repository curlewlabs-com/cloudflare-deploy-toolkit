#!/usr/bin/env bats
# Unit test for capture-helpers.sh - currently focused on
# reshape_deployments_list_json, the only helper with non-trivial
# transformation logic. The capture_* helpers are pure wrangler wrappers
# whose behavior is exercised end-to-end by the consumer's deploy-script
# test against committed fixtures.
#
# Why test the reshape at the unit level: it owns the .[-1] vs .[0]
# slot-mapping invariant - the original cron-limit incident the toolkit
# was lifted from was made worse by a reshape bug that silently swapped
# the LATEST and OLDER slots, sending the recovery rollback at the
# wrong version. A regression here is invisible until the consumer's
# mock test fails on the wrangler rollback target - too far away from
# the cause. A unit test catches it on the first failing assertion.

setup() {
  TEST_DIR="$(cd "$(dirname "$BATS_TEST_FILENAME")" && pwd)"
  HARNESS_DIR="$(cd "$TEST_DIR/.." && pwd)"
  # shellcheck disable=SC1091
  . "$HARNESS_DIR/lib/test-slots.sh"
  # shellcheck disable=SC1091
  . "$HARNESS_DIR/lib/capture-helpers.sh"
  TMP=$(mktemp -d)
}

teardown() {
  rm -rf "$TMP"
}

# Build a synthetic deployments-list array with N entries. Each entry has a
# distinctive `source` and `strategy` value (so source/strategy preservation
# can be asserted) and a placeholder version_id that will be overwritten by
# the reshape's slot-UUID mapping.
synth_deployments() {
  local count="$1"
  local i=0
  printf '['
  while [ "$i" -lt "$count" ]; do
    if [ "$i" -gt 0 ]; then printf ','; fi
    printf '{
      "id": "real-id-%d",
      "source": "wrangler-source-%d",
      "strategy": "percentage-%d",
      "author_email": "real-author-%d@example.com",
      "annotations": {
        "workers/message": "real deploy message %d (sha=deadbeef%d)",
        "workers/triggered_by": "deployment"
      },
      "versions": [
        {"version_id": "real-version-id-%d", "percentage": 100}
      ],
      "created_on": "2025-01-%02dT12:34:56.789012Z"
    }' "$i" "$i" "$i" "$i" "$i" "$i" "$i" $((i + 1))
    i=$((i + 1))
  done
  printf ']\n'
}

# Helper for the common reshape invocation. Wraps the positional args
# so each test only needs to set $TMP/in.json and call this.
reshape() {
  reshape_deployments_list_json \
    "$TMP/in.json" \
    "$WRANGLER_HARNESS_LATEST_UUID" \
    "$WRANGLER_HARNESS_OLDER_UUID" \
    "$WRANGLER_HARNESS_TEST_TIMESTAMP" \
    "$WRANGLER_HARNESS_TEST_EMAIL"
}

# ----------------------------------------------------------------------
# 5-element input -> 2-element output with slot UUIDs in the right
# positions. The .[-1] entry must end up at output[1] with the LATEST slot;
# the .[-2] entry must end up at output[0] with the OLDER slot. This is the
# core invariant the original cron-limit incident depended on: a
# .[0]/.[-1] swap would silently retarget the recovery rollback.
# ----------------------------------------------------------------------
@test "5-element input reshaped with correct slot mapping" {
  synth_deployments 5 > "$TMP/in.json"
  run reshape
  [ "$status" -eq 0 ]

  echo "$output" > "$TMP/out.json"
  [ "$(jq 'length' < "$TMP/out.json")" = "2" ]
  [ "$(jq -r '.[1].versions[0].version_id' < "$TMP/out.json")" = "$WRANGLER_HARNESS_LATEST_UUID" ]
  [ "$(jq -r '.[0].versions[0].version_id' < "$TMP/out.json")" = "$WRANGLER_HARNESS_OLDER_UUID" ]
}

@test "id pinned to fixed sentinel values" {
  synth_deployments 5 > "$TMP/in.json"
  run reshape
  [ "$status" -eq 0 ]
  echo "$output" > "$TMP/out.json"
  [ "$(jq -r '.[0].id' < "$TMP/out.json")" = "00000000-0000-0000-0000-aaaaaaaaaaaa" ]
  [ "$(jq -r '.[1].id' < "$TMP/out.json")" = "00000000-0000-0000-0000-bbbbbbbbbbbb" ]
}

@test "author_email and created_on pinned to test values" {
  synth_deployments 5 > "$TMP/in.json"
  run reshape
  [ "$status" -eq 0 ]
  echo "$output" > "$TMP/out.json"
  [ "$(jq -r '.[0].author_email' < "$TMP/out.json")" = "$WRANGLER_HARNESS_TEST_EMAIL" ]
  [ "$(jq -r '.[0].created_on' < "$TMP/out.json")" = "$WRANGLER_HARNESS_TEST_TIMESTAMP" ]
}

@test "annotations.workers/message scrubbed to fixture" {
  synth_deployments 5 > "$TMP/in.json"
  run reshape
  [ "$status" -eq 0 ]
  echo "$output" > "$TMP/out.json"
  [ "$(jq -r '.[0].annotations."workers/message"' < "$TMP/out.json")" = "fixture" ]
}

# source/strategy PASS THROUGH from the corresponding input entries.
# Synthetic input puts distinctive values per element so we can confirm
# .[-2] -> output[0] and .[-1] -> output[1].
@test "source and strategy preserved from corresponding input entries" {
  synth_deployments 5 > "$TMP/in.json"
  run reshape
  [ "$status" -eq 0 ]
  echo "$output" > "$TMP/out.json"
  [ "$(jq -r '.[0].source' < "$TMP/out.json")" = "wrangler-source-3" ]
  [ "$(jq -r '.[1].source' < "$TMP/out.json")" = "wrangler-source-4" ]
  [ "$(jq -r '.[1].strategy' < "$TMP/out.json")" = "percentage-4" ]
}

# Real SHA-shaped strings from the synthetic input must be GONE.
# This catches any future regression where annotations get preserved
# instead of pinned.
@test "no SHA-shaped strings or messages leak from input annotations" {
  synth_deployments 5 > "$TMP/in.json"
  run reshape
  [ "$status" -eq 0 ]
  [[ "$output" != *"deadbeef"* ]]
  [[ "$output" != *"real deploy message"* ]]
}

# ----------------------------------------------------------------------
# Boundary: exactly 2-element input. Same slot mapping should
# apply - the older becomes .[0], the newer becomes .[1].
# ----------------------------------------------------------------------
@test "2-element input (boundary) reshapes correctly" {
  synth_deployments 2 > "$TMP/in.json"
  run reshape
  [ "$status" -eq 0 ]
  echo "$output" > "$TMP/out.json"
  [ "$(jq 'length' < "$TMP/out.json")" = "2" ]
  [ "$(jq -r '.[0].source' < "$TMP/out.json")" = "wrangler-source-0" ]
  [ "$(jq -r '.[1].source' < "$TMP/out.json")" = "wrangler-source-1" ]
}

# ----------------------------------------------------------------------
# 1-element input -> exits non-zero with clear error message.
# ----------------------------------------------------------------------
@test "1-element input rejected with clear error" {
  synth_deployments 1 > "$TMP/in.json"
  run reshape
  [ "$status" -ne 0 ]
  [[ "$output" == *"need at least 2"* ]]
}

# ----------------------------------------------------------------------
# Empty array input -> exits non-zero.
# ----------------------------------------------------------------------
@test "empty array input rejected" {
  echo '[]' > "$TMP/in.json"
  run reshape
  [ "$status" -ne 0 ]
}

# ----------------------------------------------------------------------
# Missing input file -> exits non-zero with clear error mentioning the
# filename. Catches typos in the consumer's mktemp wiring.
# ----------------------------------------------------------------------
@test "missing input file rejected" {
  run reshape_deployments_list_json \
    "$TMP/does-not-exist.json" \
    "$WRANGLER_HARNESS_LATEST_UUID" \
    "$WRANGLER_HARNESS_OLDER_UUID" \
    "$WRANGLER_HARNESS_TEST_TIMESTAMP" \
    "$WRANGLER_HARNESS_TEST_EMAIL"
  [ "$status" -ne 0 ]
  [[ "$output" == *"does not exist"* ]]
}

# ----------------------------------------------------------------------
# Missing required arg -> usage error.
# ----------------------------------------------------------------------
@test "missing positional arg rejected with usage" {
  synth_deployments 3 > "$TMP/in.json"
  run reshape_deployments_list_json \
    "$TMP/in.json" \
    "$WRANGLER_HARNESS_LATEST_UUID" \
    "" \
    "$WRANGLER_HARNESS_TEST_TIMESTAMP" \
    "$WRANGLER_HARNESS_TEST_EMAIL"
  [ "$status" -ne 0 ]
  [[ "$output" == *"usage:"* ]]
}

# ----------------------------------------------------------------------
# WRANGLER_HARNESS_DEPLOYMENTS_ANNOTATIONS override accepts a JSON object
# literal and substitutes it into the output.
# ----------------------------------------------------------------------
@test "annotations override via env var" {
  synth_deployments 3 > "$TMP/in.json"
  WRANGLER_HARNESS_DEPLOYMENTS_ANNOTATIONS='{"workers/message":"override","custom_field":"yes"}' \
    run reshape
  [ "$status" -eq 0 ]
  echo "$output" > "$TMP/out.json"
  [ "$(jq -r '.[0].annotations."workers/message"' < "$TMP/out.json")" = "override" ]
  [ "$(jq -r '.[0].annotations.custom_field' < "$TMP/out.json")" = "yes" ]
}

# ----------------------------------------------------------------------
# Idempotency. Running the helper twice on the same input must produce
# byte-identical output. This is what the consumer's idempotency check
# ultimately depends on.
# ----------------------------------------------------------------------
@test "idempotency: byte-identical output across runs" {
  synth_deployments 4 > "$TMP/in.json"
  run reshape
  [ "$status" -eq 0 ]
  echo "$output" > "$TMP/out1.json"
  run reshape
  [ "$status" -eq 0 ]
  echo "$output" > "$TMP/out2.json"
  cmp -s "$TMP/out1.json" "$TMP/out2.json"
}

# ----------------------------------------------------------------------
# The reshape writes each entry as one version at 100%, so an entry that does
# not read that way must be refused, not normalized: `[{},{}]` used to come out
# as a valid fixture, hiding the schema drift a re-capture exists to show.
# ----------------------------------------------------------------------
@test "entries without a single serving version are refused" {
  for doc in \
    '[{},{}]' \
    '[{"versions":[]},{"versions":[{"version_id":"a","percentage":100}]}]' \
    '[{"versions":[{"version_id":"a","percentage":50},{"version_id":"b","percentage":50}]},{"versions":[{"version_id":"c","percentage":100}]}]' \
    '[{"versions":[{"percentage":100}]},{"versions":[{"version_id":"c","percentage":100}]}]'
  do
    printf '%s\n' "$doc" > "$TMP/in.json"
    run reshape
    [ "$status" -ne 0 ]
    [[ "$output" == *"must each serve one version at 100%"* ]]
  done
}
