# shellcheck shell=sh
# shellcheck disable=SC2034
# Fixed UUIDs the harness uses across mock-wrangler, capture, and test driver.
#
# Sourced (not executed) by other harness scripts and by consumer test drivers.
# The deploy pipeline meets these slots only as version IDs in the
# wrangler-shaped output mock-wrangler returns. Their job is to give the mock
# and the test driver a small, fixed alphabet so assertions can target specific slots
# ("rollback target was the LATEST version, not the OLDEST one").
#
# (SC2034: all variables in this file are intentionally defined as constants
#  for downstream sourcing - shellcheck can't see the consumers when linting
#  this file in isolation, so the warning is suppressed file-wide.)
#
# Slot conventions:
#   LATEST  - `.[-1]` of `wrangler deployments list --json`. The currently-
#             live version. Used to verify capture_previous_version_id
#             extracts the right element (regression guard for .[0] vs .[-1]).
#   OLDER   - `.[0]` of the latest/prior slice. Forces the test fixture to have
#             distinct prior and latest entries so .[-1] vs .[0] is meaningful.
#             Match-substring '11111111' tells the mock this is the maint
#             version (not the final), so it can return the right fixture.
#   FINAL   - version ID returned by the final upload step. '22222222'.
#
# If you change a slot, search the repo for the literal value first - the mock,
# capture helper, sanitizer, and test driver reference them.

WRANGLER_HARNESS_LATEST_UUID="00000000-0000-0000-0000-000000000000"
WRANGLER_HARNESS_OLDER_UUID="99999999-9999-9999-9999-999999999999"
WRANGLER_HARNESS_FINAL_UUID="22222222-2222-2222-2222-222222222222"

# Fixed values used by the sanitizer / capture pipeline for non-UUID PII slots.
WRANGLER_HARNESS_TEST_TIMESTAMP="2026-04-08T00:00:00.000000Z"
WRANGLER_HARNESS_TEST_EMAIL="test@example.com"
