#!/usr/bin/env bats
# Unit test for sanitize-wrangler-text.sh.
#
# Each @test feeds a synthetic input string containing a single class of
# variability (a UUID, a timestamp, an email, etc.) and asserts that the
# sanitizer output contains the normalized form. Cases are deliberately
# narrow so a regex regression names exactly one failing pattern.
#
# Why test the sanitizer at the unit level: it's the one piece of harness
# logic that runs at fixture-capture time, so its bugs would silently
# corrupt committed fixtures and only surface much later (or not at all).
# Catching them with a fast unit test is cheaper than re-capturing fixtures
# and discovering a leak in code review.
#
# Migrated from test-sanitize-wrangler-text.sh into bats.

setup() {
  TEST_DIR="$(cd "$(dirname "$BATS_TEST_FILENAME")" && pwd)"
  HARNESS_DIR="$(cd "$TEST_DIR/.." && pwd)"
  SANITIZER="$HARNESS_DIR/bin/sanitize-wrangler-text.sh"
  if [ ! -x "$SANITIZER" ]; then
    echo "sanitizer not executable at $SANITIZER" >&2
    return 1
  fi
}

# Helper: pipe an input string through the sanitizer with the given args.
# Used as `run sanitize_input "$input" <mode> [extra args ...]`.
sanitize_input() {
  local input="$1"
  shift
  printf '%s' "$input" | "$SANITIZER" "$@"
}

# ----------------------------------------------------------------------
# Wrangler version banner normalization.
# Catches regex regressions where a wrangler bump (4.79.0 to 4.81.0) leaks
# the new version into committed fixtures. The banner must collapse to
# "wrangler 0.0.0" with and without the "update available" parenthetical.
#
# The banner below is wrangler's own stdout, reproduced byte for byte: its
# sun-behind-cloud mark and the box-drawing rule under it are written as
# printf octal escapes, which produce those exact bytes when the test runs,
# so the fixture asserts against output wrangler really emits and the file
# itself stays ASCII.
# ----------------------------------------------------------------------
@test "version banner with update warning" {
  sun=$(printf '\342\233\205\357\270\217')
  rule=$(printf '\342\224\200%.0s' {1..45})
  input="$sun wrangler 4.79.0 (update available 4.81.0)
$rule
--dry-run: exiting now."
  run sanitize_input "$input" upload-maint
  [ "$status" -eq 0 ]
  [[ "$output" == *"wrangler 0.0.0"* ]]
  [[ "$output" != *"4.79.0"* ]]
  [[ "$output" != *"4.81.0"* ]]
  [[ "$output" != *"update available"* ]]
}

@test "version banner without update warning" {
  sun=$(printf '\342\233\205\357\270\217')
  input="$sun wrangler 5.0.0
--dry-run: exiting now."
  run sanitize_input "$input" upload-final
  [ "$status" -eq 0 ]
  [[ "$output" == *"wrangler 0.0.0"* ]]
  [[ "$output" != *"5.0.0"* ]]
}

# ----------------------------------------------------------------------
# Total Upload size normalization.
# The bundle size changes on every code commit. If left unscrubbed, every
# fixture re-capture would produce a noisy diff regardless of whether
# anything in wrangler's output format actually drifted.
# ----------------------------------------------------------------------
@test "total upload size normalized" {
  input='Total Upload: 111.75 KiB / gzip: 24.10 KiB
--dry-run: exiting now.'
  run sanitize_input "$input" upload-maint
  [ "$status" -eq 0 ]
  [[ "$output" == *"Total Upload: 0.00 KiB / gzip: 0.00 KiB"* ]]
  [[ "$output" != *"111.75"* ]]
  [[ "$output" != *"24.10"* ]]
}

# ----------------------------------------------------------------------
# --scrub-binding flag with one binding name.
# Verifies that the consumer-supplied scrub flag works for a single binding.
# The default sanitizer (no flag) does NOT scrub binding values, so this
# case also verifies the flag-driven behavior.
#
# The input value is intentionally obviously-synthetic - the scrub-binding
# feature redacts by binding NAME, not value shape, so the test is identical
# regardless of what the value looks like. Do not "improve" this with a
# realistic-looking value: the whole point of testing a sanitizer is that
# the test fixtures themselves should never contain real PII.
# ----------------------------------------------------------------------
@test "scrub-binding single name redacts value" {
  input='env.EXAMPLE_BINDING_NAME ("FAKE_TEST_BINDING_VALUE_DO_NOT_USE")
--dry-run: exiting now.'
  run sanitize_input "$input" upload-maint --scrub-binding EXAMPLE_BINDING_NAME
  [ "$status" -eq 0 ]
  [[ "$output" == *'env.EXAMPLE_BINDING_NAME ("REDACTED")'* ]]
  [[ "$output" != *"FAKE_TEST_BINDING_VALUE_DO_NOT_USE"* ]]
}

# ----------------------------------------------------------------------
# Current wrangler prints a binding unquoted, `env.NAME (value)`, and colors the
# name and the value separately. The scrub used to run before the ANSI strip
# and to match only the quoted form, so both of these passed the value
# through into a fixture.
# ----------------------------------------------------------------------
@test "scrub-binding redacts the unquoted form wrangler prints now" {
  input='env.EXAMPLE_BINDING_NAME (FAKE_TEST_BINDING_VALUE_DO_NOT_USE)      Environment Variable
--dry-run: exiting now.'
  run sanitize_input "$input" upload-maint --scrub-binding EXAMPLE_BINDING_NAME
  [ "$status" -eq 0 ]
  [[ "$output" == *'env.EXAMPLE_BINDING_NAME (REDACTED)'* ]]
  [[ "$output" != *"FAKE_TEST_BINDING_VALUE_DO_NOT_USE"* ]]
}

# A value holding a parenthesis or an escaped quote has no closing delimiter a
# pattern can trust, so the scrub runs to the end of the line; stopping at the
# first `)` or `"` would leave the value's tail in the fixture.
@test "scrub-binding redacts a value's tail past a parenthesis or quote" {
  input='env.EXAMPLE_BINDING_NAME (FAKE_PREFIX)FAKE_TAIL_DO_NOT_USE)      Environment Variable
env.OTHER_BINDING ("FAKE_PREFIX\"FAKE_QUOTED_TAIL_DO_NOT_USE")
--dry-run: exiting now.'
  run sanitize_input "$input" upload-maint --scrub-binding EXAMPLE_BINDING_NAME --scrub-binding OTHER_BINDING
  [ "$status" -eq 0 ]
  [[ "$output" == *'env.EXAMPLE_BINDING_NAME (REDACTED)'* ]]
  [[ "$output" == *'env.OTHER_BINDING ("REDACTED")'* ]]
  [[ "$output" != *"FAKE_TAIL_DO_NOT_USE"* ]]
  [[ "$output" != *"FAKE_QUOTED_TAIL_DO_NOT_USE"* ]]
}

@test "scrub-binding redacts a value wrangler colored" {
  esc=$(printf '\033')
  input="${esc}[37menv.EXAMPLE_BINDING_NAME${esc}[39m (${esc}[2mFAKE_TEST_BINDING_VALUE_DO_NOT_USE${esc}[22m)
--dry-run: exiting now."
  run sanitize_input "$input" upload-maint --scrub-binding EXAMPLE_BINDING_NAME
  [ "$status" -eq 0 ]
  [[ "$output" == *'env.EXAMPLE_BINDING_NAME (REDACTED)'* ]]
  [[ "$output" != *"FAKE_TEST_BINDING_VALUE_DO_NOT_USE"* ]]
}

# ----------------------------------------------------------------------
# Defense against making the sanitizer too aggressive: with no flag, the
# binding value passes through unchanged. The consumer is responsible for
# listing the bindings they want scrubbed.
# ----------------------------------------------------------------------
@test "binding preserved when no scrub flag" {
  input='env.SOME_UNSCRUBBED ("public-config-value")
--dry-run: exiting now.'
  run sanitize_input "$input" upload-maint
  [ "$status" -eq 0 ]
  [[ "$output" == *'env.SOME_UNSCRUBBED ("public-config-value")'* ]]
}

# ----------------------------------------------------------------------
# Repeated --scrub-binding flags accumulate.
# ----------------------------------------------------------------------
@test "scrub-binding multiple names accumulate" {
  input='env.FOO ("secret1")
env.BAR ("secret2")
env.UNSCRUBBED ("public")
--dry-run: exiting now.'
  run sanitize_input "$input" upload-maint --scrub-binding FOO --scrub-binding BAR
  [ "$status" -eq 0 ]
  [[ "$output" == *'env.FOO ("REDACTED")'* ]]
  [[ "$output" == *'env.BAR ("REDACTED")'* ]]
  [[ "$output" == *'env.UNSCRUBBED ("public")'* ]]
  [[ "$output" != *"secret1"* ]]
  [[ "$output" != *"secret2"* ]]
}

# ----------------------------------------------------------------------
# ANSI escape code stripping.
# Wrangler emits color codes even when stdout is redirected to a file
# (verified with od -c). Without this scrub, fixtures would carry literal
# ESC bytes that vary by wrangler version and break grep-based assertions.
# Uses printf to embed real ESC bytes - the regex must match them at the
# byte level, not the visual level.
# ----------------------------------------------------------------------
@test "ansi escape codes stripped" {
  input=$(printf ' \033[33m\xe2\x96\xb2 [WARNING]\033[0m experimental command\n--dry-run: exiting now.')
  run sanitize_input "$input" triggers
  [ "$status" -eq 0 ]
  [[ "$output" == *"WARNING"* ]]
  [[ "$output" == *"experimental command"* ]]
  [[ "$output" != *$'\033[33m'* ]]
  [[ "$output" != *$'\033[0m'* ]]
}

# ----------------------------------------------------------------------
# ISO 8601 timestamp normalization.
# ----------------------------------------------------------------------
@test "iso timestamp normalized" {
  input='Created: 2026-04-08T21:35:56.173855Z
--dry-run: exiting now.'
  run sanitize_input "$input" upload-maint
  [ "$status" -eq 0 ]
  [[ "$output" == *"Created: 2026-04-08T00:00:00.000000Z"* ]]
  [[ "$output" != *"21:35:56"* ]]
}

# ----------------------------------------------------------------------
# Email address scrubbing.
#
# Input is a clearly-synthetic address on the .invalid TLD (RFC 2606
# reserves .invalid specifically for this use case - it can never be a
# real domain). Do not replace with a real-looking address.
# ----------------------------------------------------------------------
@test "email address scrubbed" {
  input='Author: nobody@example.invalid
--dry-run: exiting now.'
  run sanitize_input "$input" upload-maint
  [ "$status" -eq 0 ]
  [[ "$output" == *"test@example.com"* ]]
  [[ "$output" != *"nobody"* ]]
  [[ "$output" != *"example.invalid"* ]]
}

# ----------------------------------------------------------------------
# Generic 36-char dash UUID scrubbing (defense in depth).
# ----------------------------------------------------------------------
@test "stray uuid scrubbed" {
  input='binding-id: deadbeef-cafe-1234-5678-9abcdef01234
--dry-run: exiting now.'
  run sanitize_input "$input" upload-maint
  [ "$status" -eq 0 ]
  [[ "$output" == *"00000000-0000-0000-0000-000000000000"* ]]
  [[ "$output" != *"deadbeef-cafe-1234-5678-9abcdef01234"* ]]
}

# ----------------------------------------------------------------------
# 32-char hex (account ID shape) scrubbing (defense in depth).
# ----------------------------------------------------------------------
@test "account id scrubbed" {
  input='account: 1234567890abcdef1234567890abcdef
--dry-run: exiting now.'
  run sanitize_input "$input" upload-maint
  [ "$status" -eq 0 ]
  [[ "$output" == *"00000000000000000000000000000000"* ]]
  [[ "$output" != *"1234567890abcdef1234567890abcdef"* ]]
}

# ----------------------------------------------------------------------
# Worker Version ID injection per slot.
# ----------------------------------------------------------------------
@test "upload-maint injects 11111111 slot" {
  run sanitize_input '--dry-run: exiting now.' upload-maint
  [ "$status" -eq 0 ]
  [[ "$output" == *"Worker Version ID: 11111111-1111-1111-1111-111111111111"* ]]
  [[ "$output" != *"--dry-run: exiting now."* ]]
}

@test "upload-final injects 22222222 slot" {
  run sanitize_input '--dry-run: exiting now.' upload-final
  [ "$status" -eq 0 ]
  [[ "$output" == *"Worker Version ID: 22222222-2222-2222-2222-222222222222"* ]]
  [[ "$output" != *"--dry-run: exiting now."* ]]
}

# ----------------------------------------------------------------------
# triggers mode does NOT inject a Worker Version ID.
# ----------------------------------------------------------------------
@test "triggers mode preserves dry-run trailer" {
  run sanitize_input '--dry-run: exiting now.' triggers
  [ "$status" -eq 0 ]
  [[ "$output" == *"--dry-run: exiting now."* ]]
  [[ "$output" != *"Worker Version ID:"* ]]
}

# ----------------------------------------------------------------------
# Invalid mode rejected.
# ----------------------------------------------------------------------
@test "invalid mode rejected" {
  run sanitize_input "anything" garbage-mode
  [ "$status" -ne 0 ]
}

# ----------------------------------------------------------------------
# Missing mode prints usage.
# ----------------------------------------------------------------------
@test "missing mode rejected" {
  run bash -c 'printf "anything" | "$1"' bash "$SANITIZER"
  [ "$status" -ne 0 ]
}

# ----------------------------------------------------------------------
# --scrub-binding with no value rejected.
# ----------------------------------------------------------------------
@test "--scrub-binding requires arg" {
  run sanitize_input "anything" upload-maint --scrub-binding
  [ "$status" -ne 0 ]
}
