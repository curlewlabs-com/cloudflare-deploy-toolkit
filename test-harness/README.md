# test-harness

Test-time tooling for shell scripts that orchestrate Cloudflare Workers deploys
via the `wrangler` CLI. Lets you exercise every documented failure path of your
deploy script (cron-limit error, DB-lock failure, migration failure, postflight
failure, etc.) without ever calling real wrangler.

## How it fits together

```
                +----------------------------------+
                |  consumer's deploy-safe.sh       |
                |  (~30-line wrapper that hands    |
                |   off to ../deploy-lib/ - that's |
                |   where the orchestration logic  |
                |   lives, in either language)     |
                +------------+---------------------+
                             | $WRANGLER (env var)
                             v
                +----------------------------------+
                |  bin/mock-wrangler.sh            |
                |  - classifies subcommands        |
                |  - logs every call               |
                |  - injects permanent failures    |
                |    via MOCK_FAIL_AT              |
                |  - injects transient failures    |
                |    via MOCK_TRANSIENT_FAIL_AT    |
                |  - reads fixtures from           |
                |    $FIXTURE_DIR                  |
                +------------+---------------------+
                             | cat
                             v
        +----------------------------------------------+
        |  consumer's test/fixtures/wrangler/          |
        |  +-- deployments_list.json   (parsed by jq)  |
        |  +-- versions_view.json      (serving build) |
        |  +-- upload_maint.txt        (Worker Ver ID) |
        |  +-- upload_final.txt        (Worker Ver ID) |
        |  +-- bookmark.json           (parsed by jq)  |
        |  +-- triggers.txt            (sanitized)     |
        |  +-- *.txt                   (empty markers) |
        +----------------------------------------------+
                             ^
                             | written by
        +--------------------+----------------------+
        |  consumer's capture-wrangler-fixtures.sh  |
        |  +-- sources lib/capture-helpers.sh       |
        |  +-- pipes through bin/sanitize-          |
        |  |   wrangler-text.sh with --scrub-       |
        |  |   binding for consumer-specific PII    |
        |  +-- reshapes deployments_list with       |
        |      slot UUIDs from lib/test-slots.sh    |
        +-------------------------------------------+
```

## What lives where

- **`bin/sanitize-wrangler-text.sh`** - [OK]: Pure text transform; takes
  `--scrub-binding NAME` for binding values to scrub
- **`bin/mock-wrangler.sh`** - [OK]: Classifies wrangler subcommands and reads
  fixtures from `$FIXTURE_DIR`
- **`bin/check-fixture-purity.sh`** - [OK]: Generic patterns built in (UUIDs,
  account hex, emails, timestamps, ANSI). Takes optional consumer literals file
- **`lib/test-slots.sh`** - [OK]: Shared slot UUID constants - sourced by mock,
  sanitizer, and consumer test driver
- **`lib/capture-helpers.sh`** - [OK]: Sourceable wrappers for safe wrangler
  subcommands (dry-run / read-only only)
- **`test/test-*.bats`, `test/test_*.py`** - [OK]: The suites. Which risk each
  one owns is sec. Test layers below; its header says what its cases guard
- **`test/example-deploy-wrapper.sh`** - [OK]: Minimal example consumer wrapper,
  what the end-to-end suites drive; doubles as a working reference for someone
  writing their first wrapper
- **`test/example-hook.sh`** - [OK]: The no-op hook command the example wrapper
  points its preflight, migration, and post-flight hooks at
- **`test/example-fixtures/*`** - [OK]: Generic mock-wrangler fixtures the
  end-to-end suites read
- **`test/check.sh`** - [OK]: Runs shellcheck, `bats *.bats`, the `test_*.py`
  gate suites, and a vendored-path smoke for the harness entrypoint
- **consumer's `capture-wrangler-fixtures.sh`** - [OK]: Knows which DB, which
  bindings to scrub, the slot-UUID reshape
- **consumer's `test-deploy-safe-mock.bats`** - [OK]: Tests _the consumer's_
  deploy script's specific failure paths
- **consumer's `test-fixture-purity.bats`** - [OK]: Thin wrapper passing the
  consumer's literals file to the harness check
- **consumer's `forbidden-literals.txt`** - [OK]: Consumer-specific PII that the
  generic patterns don't catch

## Test layers

The record of which suite owns which risk. Each suite's header says what its
cases guard; this section says which suite to read for a risk, not what its
cases are.

Toolkit self-tests, run in the toolkit's CI by `test/check.sh`:

- **The windowed and windowless bodies' order, binding, and recovery**
  (`test-deploy-pipeline-smoke.bats`) - the happy path runs every step in order
  on each body, the serving-build bind and the split-serving refusal abort
  before any mutation, and Worker rollback and full D1/Worker/unlock recovery
  fire at the right state-machine boundaries
- **The uploaded-runtime check** (`test-deploy-pipeline-runtime.bats`) - the
  read-back of an upload comes before any window, pause, or promote on the
  windowed and windowless bodies, each refusal names its cause so a failed read
  never reads as a mismatch, and an unset `DEPLOY_EXPECTED_RUNTIME` makes no
  call
- **The relocation body** (`test-deploy-pipeline-relocation.bats`) - its
  ordering, the runtime refusal before the source is fenced included, and the
  source and target lock states between its failure boundaries
- **The classified-transient retry** (`test-deploy-pipeline-retry.bats`) -
  recovers on representative allowlisted shapes, fails fast on non-retryable
  ones including the edge render whose header a 5xx and a 4xx share, exhausted
  budget propagates cleanly, the recovery rollback also retries, callers that
  parse a captured document keep clean stdout on retry success, and
  `total_wrangler_retries` is recorded accurately
- **One module or gate on its own** (`test_*.py`) - a decision the pipeline
  makes, a gate under `../bin/`, or the install roster, driven against real
  documents or real throwaway repos with no deploy around it; the module's
  docstring names what it guards
- **The mock's wiring** (`test-mock-wrangler-smoke.bats`) - the mock classifies
  a call and reads its fixture back
- **Sanitizer regressions** (`test-sanitize-wrangler-text.bats`) - each scrub
  pattern as a unit case
- **Reshape regressions** (`test-capture-helpers.bats`) - slot-UUID mapping in
  `reshape_deployments_list_json` (the original `.[0]` vs `.[-1]` swap that
  motivated extracting this helper)

Consumer CI:

- **Fixture purity** (`check-fixture-purity.sh`, generic patterns plus consumer
  literals) - anything that bypassed the sanitizer and ended up in committed
  fixtures (account IDs, real UUIDs, real emails, real timestamps, ANSI codes,
  leaked SHAs, real wrangler version strings)
- **Consumer mock-driven orchestration** (consumer's
  `test-deploy-safe-mock.bats`) - the consumer's wrapper-specific
  failure-handling paths against committed fixtures captured from the consumer's
  own wrangler config

The toolkit layers verify the toolkit in isolation; the consumer layers verify
the consumer's wiring against its own captured fixtures. A change to a body's
step order is held by the end-to-end suite that owns that step above, and the
unit layers guard against those suites silently lying because the fixtures or
the helpers drifted.

## Slot UUIDs

The harness uses a small fixed alphabet of UUIDs across the mock, capture, and
test driver. They're defined in `lib/test-slots.sh`:

- **`WRANGLER_HARNESS_LATEST_UUID`** - `00000000-0000-0000-0000-000000000000`:
  `.[-1]` of deployments list - current version, rollback target
- **`WRANGLER_HARNESS_OLDER_UUID`** - `99999999-9999-9999-9999-999999999999`:
  `.[0]` of the latest/prior slice - second-to-latest, forcing distinct latest
  and prior fixture entries
- **`WRANGLER_HARNESS_MAINT_UUID`** - `11111111-1111-1111-1111-111111111111`:
  Returned by upload-maint, mock matches by `*11111111*` substring
- **`WRANGLER_HARNESS_FINAL_UUID`** - `22222222-2222-2222-2222-222222222222`:
  Returned by upload-final, mock matches by `*22222222*` substring

Why these specific values: they're trivially distinguishable (regex-wise) and
the all-zero / all-9 / all-1 / all-2 patterns make it impossible to confuse a
slot UUID with a real one in a fixture diff.

The consumer's deploy script never references these constants - it just greps
for whatever wrangler emits. The slots exist purely to give the test driver a
way to make assertions like "wrangler rollback was called with the LATEST slot,
not the OLDER slot" (which catches `.[0]` vs `.[-1]` regressions).

## Onboarding a new consumer

1. **Capture fixtures** by writing your own `capture-wrangler-fixtures.sh` that
   sources `lib/capture-helpers.sh` and writes to your fixture directory. Your
   script needs to know:
   - `WRANGLER_DB` - your D1 DB name
   - `--scrub-binding NAME` - the `wrangler.toml` binding names whose values are
     PII / environment-specific (one `--scrub-binding` per binding)
   - The output path for the captured fixtures

2. **Wire up the mock test** by writing a `.bats` driver that:
   - Sets `WRANGLER=$HARNESS_DIR/bin/mock-wrangler.sh`
   - Sets `FIXTURE_DIR=$YOUR_FIXTURE_DIR`
   - Sets `MOCK_LOG_FILE=$BATS_TEST_TMPDIR/calls.log`
   - Sets `MOCK_SCHEMA_FILE` and `MOCK_DATA_SEED_FILE` when the consumer's real
     postflight hook reads the current schema and lookup-row snapshots instead
     of stubbing them
   - Sets `MOCK_SCHEMA_VERSION_FILE` (pointing at the same `version.txt` the
     consumer ships) when the postflight hook verifies `_schema_meta`; without
     it the mock falls back to a fixed `version=1` and a real version-bump
     regression would be invisible
   - Uses `MOCK_SCHEMA_STATE_MODE=empty_after_apply` in one regression case if
     the wrapper must prove that an empty post-apply DB fails closed
   - Has one `@test` per `MOCK_FAIL_AT` value (permanent failures) and asserts
     on the call log
   - Optionally adds `@test` cases using
     `MOCK_TRANSIENT_FAIL_AT=<step>:<count>:<pattern>` to prove the retry helper
     covers the consumer's wrapped wrangler calls - patterns are documented at
     the top of `bin/mock-wrangler.sh`

   A typical driver defines `run_deploy` / `assert_step` / `refute_step` helpers
   wrapping the deploy invocation, then has one case per documented failure
   point in the pipeline plus one strict-postflight empty-DB case when the
   consumer's real postflight hook enforces schema presence after apply.

3. **Wire up fixture purity** by writing a thin `.bats` wrapper that calls
   `bin/check-fixture-purity.sh` with your fixture dir and a
   `forbidden-literals.txt` file containing your consumer-specific PII strings
   (binding values, real account IDs, etc.). The harness ships the generic
   patterns (UUIDs, account hex, emails, timestamps); you only need to add the
   strings unique to your deployment.

4. **Run from CI** by chaining `test-harness/test/check.sh` and your wrappers
   into your project's check suite (e.g. `bats your-test-dir/*.bats`).

A consumer whose deploy hooks can run against a real local D1 may prefer that
to the mock-driven layers: test the hooks against the real database, and let the
toolkit's own smoke and retry suites cover the pipeline bodies. The pattern
above is for a wrapper that cannot be exercised against a real database.

## Re-capturing fixtures

When to re-run:

- After a wrangler version bump (`bunx wrangler --version` shows new value)
- After a Cloudflare-side schema change to `deployments list --json` or
  `d1 time-travel info --json`
- If `check-fixture-purity.sh` flags something the sanitizer should have caught
- **Never on a regular cadence** - re-capturing on every PR creates
  change-detector noise on every wrangler bump

After re-capture:

```sh
git diff <your-fixture-dir>             # review what changed
bats <your-test-dir>/*.bats             # mock + purity must still pass
```

A clean `git diff` after re-capture means nothing drifted. The diffs below are
red flags and need code changes, not just a fixture update:

1. **`Worker Version ID:` line removed or renamed** - the upload step reads that
   exact prefix out of wrangler's output. If wrangler renames it, the parse
   breaks silently in production.
2. **`deployments list --json` schema shape changed** - the capture step reads
   the newest deployment's single serving version out of that document
   (`deploy-lib/deployments_list.py` owns which shapes it refuses). If the shape
   moves, every capture refuses and no deploy starts.

## Test framework

Relocation hook-timeout cases use `test/relocation_deadline.py` to order expiry
after the hanging hook publishes its process IDs. The driver runs the real
pipeline and expires the real subprocess wait; process-group termination and
lock recovery remain production code. The setup backstop is deliberately
separate from the timeout being asserted, so runner contention cannot spend the
asserted budget before the fixture exists. Ordinary wrapper cases still cover
the relocation's wall-clock budget between hooks. Its startup-cancellation mode
sends a real termination signal after the hook publishes its process IDs but
before the spawn call returns to the pipeline. The same process-liveness and
source-fence assertions then cover cancellation before the parent can adopt the
child, without relying on scheduler timing.

The harness uses [bats-core](https://github.com/bats-core/bats-core) for its
fixture- and pipeline-oriented self-tests and Python's `unittest` for the `bin/`
gate suites. Consumers should use bats for their `*.bats` drivers too. Install:

- **Mac:** `brew install bats-core shellcheck jq python@3.14 grep`
- **WSL / Debian / Ubuntu:** `sudo apt install -y bats shellcheck jq python3.14`
  (Python 3.14 from the deadsnakes PPA where the release does not carry it),
  then `sudo ln -sf /bin/grep /usr/local/bin/ggrep`
- **Other Linux:** install `bats`, `shellcheck`, `jq`, and `python3` via your
  distro's package manager (or `cargo install` / GitHub releases), and put GNU
  grep on PATH as `ggrep`

check-fixture-purity.sh additionally requires `ggrep`: it scans fixtures with a
word-bounded pattern POSIX ERE does not define, and refuses to run rather than
report a clean scan it never performed. macOS gets it from the `grep` formula;
on Linux, symlink `ggrep` to the system GNU `grep`. The apt line names
`/bin/grep` outright because `$(command -v grep)` prints alias text, not a path,
wherever `grep` is aliased, which Ubuntu's stock `~/.bashrc` does. It links with
`-f` so it also replaces a dangling `ggrep` that such a lookup left behind.

The test runner falls through to the `test-harness/test/check.sh` shell script
for the shellcheck step and the vendored-path smoke for the harness entrypoint.
Assertion-bearing tests are split by what they drive: the wrangler fixture flows
are `.bats` files, and the `bin/` gates are Python suites beside them, each
pinning one gate through its real executable. `test/check.sh` prints install
guidance when `bats` or `python3` is missing from PATH, so a config issue
surfaces with actionable guidance instead of a later "command not found".

The Bats suites assert with bare `[[ ]]` tests under errexit. macOS
`/bin/bash` 3.2 runs on past a failing `[[ ]]`, so under it a suite would fail a
test only on its last assertion and pass whatever the code under test does.
`test/check.sh` probes the `bash` on `PATH` for this and refuses to run the
suites under one that does not stop; on macOS, `brew install bash` puts a
current one first.
