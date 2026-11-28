# Toolkit tests

This directory holds the repository's tests for the deploy pipeline, gates, and
fixture tooling. The pipeline tests replay Wrangler fixtures through a test
double, so they need no Cloudflare account.

## Run locally

From the repository root, run the same entry point as CI.

```sh
./test-harness/test/check.sh
```

Put Bats, Python 3.14, `jq`, a current `bash`, and GNU grep as `ggrep` on
`PATH`. On macOS, install `bats-core`, `python@3.14`, `jq`, `bash`, and `grep`
with Homebrew. Put Homebrew's Bash first on `PATH`. The system Bash can continue
past a failed `[[ ]]` assertion, which makes the test result unreliable. On
Linux, expose the system GNU grep as `ggrep` if that name is absent.

## Test layers

- Bats drives the real pipeline through `test/example-deploy-wrapper.sh` and
  [the Wrangler double](bin/mock-wrangler.sh). Its failure-injection settings
  let the suites check what runs after a failed deploy step. See
  [the pipeline guide](../deploy-lib/README.md) for the expected recovery.
- Python's `unittest` covers the pipeline's individual decisions and the
  gates under [`../bin/`](../bin/). Run the relevant `test/test_*.py` file with
  Python 3.14 to iterate on one suite.
- The runner also executes the suites from a relocated toolkit tree to catch
  assumptions about the checkout path.

## Fixtures

Start from `test/example-fixtures/` when adding a pipeline case. Re-capture
only when a new test needs a response or Wrangler changes its output. Use
[the capture helpers](lib/capture-helpers.sh) for dry-run or read-only calls and
[the sanitizer](bin/sanitize-wrangler-text.sh) to scrub environment values.
The fixture UUIDs in [test-slots.sh](lib/test-slots.sh) make the older, serving,
and newly uploaded versions distinguishable in a call log.

Review fixture diffs for operational data before committing, then run
[the fixture purity scanner](bin/check-fixture-purity.sh) on the changed fixture
set. If an upload's `Worker Version ID:` prefix or the deployments JSON shape
changes, check the production parser before updating the fixture.
