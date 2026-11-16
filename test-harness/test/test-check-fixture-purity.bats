#!/usr/bin/env bats
# The fixture-purity scan is the last check before a captured fixture is
# committed, so its failure mode that matters is passing a fixture it never
# read: a path with whitespace split into fragments, or a file grep could not
# open, used to read as clean.

setup() {
  TEST_DIR="$(cd "$(dirname "$BATS_TEST_FILENAME")" && pwd)"
  SCAN="$TEST_DIR/../bin/check-fixture-purity.sh"
  FIXTURES="$BATS_TEST_TMPDIR/fixtures"
  mkdir -p "$FIXTURES"
  printf '%s\n' "Worker Version ID: 11111111-1111-1111-1111-111111111111" > "$FIXTURES/clean.txt"
}

@test "a clean fixture set passes" {
  run "$SCAN" "$FIXTURES"
  [ "$status" -eq 0 ]
}

@test "a leak in a file whose name holds a space is found" {
  printf '%s\n' "id 12345678-1234-1234-1234-123456789abc" > "$FIXTURES/with space.json"
  run "$SCAN" "$FIXTURES"
  [ "$status" -ne 0 ]
  [[ "$output" == *"with space.json"* ]]
}

@test "a fixture grep cannot read fails the scan" {
  [ "$(id -u)" -ne 0 ] || skip "root reads every file"
  printf '%s\n' "anything" > "$FIXTURES/locked.txt"
  chmod 000 "$FIXTURES/locked.txt"
  run "$SCAN" "$FIXTURES"
  chmod 644 "$FIXTURES/locked.txt"
  [ "$status" -ne 0 ]
  [[ "$output" == *"could not scan"*"locked.txt"* ]]
}

@test "a forbidden literal is found in a spaced name too" {
  printf '%s\n' "my-secret-db" > "$FIXTURES/a b.txt"
  printf '%s\n' "my-secret-db" > "$BATS_TEST_TMPDIR/literals.txt"
  run "$SCAN" "$FIXTURES" "$BATS_TEST_TMPDIR/literals.txt"
  [ "$status" -ne 0 ]
  [[ "$output" == *"a b.txt"* ]]
}
