# Deploy pipeline

Run the pipeline for an existing Worker with a single version serving all
traffic. A fresh Worker needs its first deployment separately. See the
[toolkit limits](../README.md#limits).

## Upgrading and serialization

Serialize deployments for each environment from preflight through failure
recovery. Operate dashboard traffic changes and manual migrations inside that
same exclusion. The version checks detect a changed serving version but cannot
make a check and the next remote mutation atomic.

Before upgrading a lock-aware wrapper, reconcile any outstanding maintenance
deployment or database relocation using its original release. Confirm which
Worker serves traffic and which database accepts writes. The new wrapper must
select `DEPLOY_PIPELINE_MODE=windowless-v1` and provide `WINDOWLESS_PREFLIGHT_CMD`.
Have that command refuse an unreadable or held database fence while any old
deployment can still set it. Once all old writers have retired and the final
fence is verified clear, the hook checks the live schema and compatibility of
the migration. Verify every committed intermediate state can serve every Worker
version still executing. A deployment showing the captured version at full
traffic does not prove older requests have finished. Defer contract migrations
until the earlier code has drained across propagation and in-flight work.
The command receives the environment followed by the captured
Worker version ID. Read that immutable version's bindings and compatibility
metadata. A check that also reads a live endpoint must verify that its response
belongs to the captured version. Refuse a changed or unreadable database
binding. Resolve the database the migration hook will actually write and
require its ID to match the captured binding and the candidate binding.
Even for code-only deployments, verify that both versions can read and process
the writes the other version can make throughout promotion and recovery.
Check trigger compatibility in both directions: serving code can receive new
routes, schedules or queue events before promotion, and the candidate can
still receive the previous triggers while changes propagate. Matching schemas on
different databases do not establish that writes will reach the candidate.
Older releases reject the new mode, so a mismatched wrapper and pin abort. A preflight failure stops before any mutation.

## Deploy order and recovery

1. The pipeline captures the serving version for rollback. When given an
   expected serving commit it verifies the immutable upload message.
2. Your preflight checks writes, migrations, bindings and triggers for compatibility with
   the candidate and every version that can still execute. The pipeline rechecks it before changing triggers.
3. Wrangler synchronizes the triggers. The pipeline rechecks the serving version
   before allowing your migration to run.
4. After migration, it uploads the new version and verifies its runtime if
   configured. It rechecks the serving version immediately before promotion
   and before retrying a rejected lookup of the uploaded version.
5. Your postflight checks the new serving deployment.

A failed migration leaves the old Worker serving. Inspect the database before
retrying. Trigger changes can already be live even if a later phase fails.
Inspect the triggers Wrangler attempted to change, including queue consumers,
routes and schedules. Reconcile the active configuration with the serving code.
Reconcile them explicitly after any rollback, which changes only the version.
A failed promotion whose submission might have applied triggers
rollback to the captured version. The pipeline leaves the traffic split alone only if every failed attempt
was a rejected version lookup before submission. A later rejected lookup
cannot clear an earlier ambiguous submission. If rollback reports failure,
read the serving version and traffic split before changing traffic. A lost
response can follow a successful rollback. A successful promotion
followed by a postflight failure leaves the new version live for investigation.
Final promotion verifies the recorded serving version again, including before
each retry for a rejected version lookup. All migrations must keep both
versions compatible. The pipeline never restores a database.

## Consumer wrapper

A minimal consumer wrapper is ~30 lines of boilerplate plus a few lines per
hook.

```sh
#!/bin/sh
set -eu

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVICE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd "$SERVICE_DIR/../.." && pwd)"
DEPLOY_TOOLKIT_DIR="$REPO_ROOT/vendor/cloudflare-deploy-toolkit"

# === required config (exported: a subprocess reads its environment) ===
export DEPLOY_WRANGLER_ENV_STAGING="staging"
export DEPLOY_WRANGLER_ENV_PRODUCTION="production"

# Hook commands receive the environment; preflight also receives the version ID.
export DEPLOY_PIPELINE_MODE=windowless-v1
# Supply this consumer-owned hook before adopting the pipeline. It verifies
# live migration compatibility and refuses a held or unreadable legacy fence.
export WINDOWLESS_PREFLIGHT_CMD="$SCRIPT_DIR/windowless-preflight.sh"
# A migrate command is REQUIRED and is checked at startup. Use `true` when
# routine deploys must preserve remote DB contents.
export MIGRATE_CMD="true"
export POSTFLIGHT_CMD="$SCRIPT_DIR/check-schema-live.sh --require-schema"

# === run ===
cd "$SERVICE_DIR"
exec python3.14 "$DEPLOY_TOOLKIT_DIR/deploy-lib/safe_deploy_pipeline.py" "$@"
```

The properties of that wrapper are worth naming, and each follows from the
pipeline being a program rather than a library:

- **Config is exported.** A subprocess reads its environment; a shell variable
  the wrapper merely assigned is invisible to it.
- **Hooks are commands.** The pipeline appends the environment and, for preflight,
  the captured version ID. A command may carry its own leading arguments (the example
  above passes `--require-schema` before the env). The strings are split with
  `shlex`, so a path containing a space is expressible if it is quoted.
- **The interpreter is the wrapper's choice.** The pipeline is the process the
  wrapper starts, so the wrapper picks the Python - prefer a named version, and
  fall back to `python3` only where the platform's bare `python3` is new enough
  (Apple's is not). The pipeline itself reads no `DEPLOY_TOOLKIT_DIR` - the
  modules import each other from the directory they share, and the variable
  above is the wrapper's own path handle, which `wrangler-retry.sh` is what
  actually requires be set.

## API

### Configuration and hooks

Export `DEPLOY_WRANGLER_ENV_STAGING` or `DEPLOY_WRANGLER_ENV_PRODUCTION` for
the requested environment. `MIGRATE_CMD` is required. Use `true` when the
deployment has no migration. `WINDOWLESS_PREFLIGHT_CMD` is required and must
verify every database state a partial migration can leave is compatible with
every version that can still execute. Verify two-way write compatibility even
when the migration command is `true`. `POSTFLIGHT_CMD` is optional.
Each command is split using shell quoting and receives the target environment
after any configured arguments. Preflight also receives the immutable serving
version ID after the environment. Hooks inherit the wrapper's environment. A nonzero exit
fails the deployment.

Set `DEPLOY_PIPELINE_MODE=windowless-v1` explicitly. A missing, empty or unknown
mode refuses before any remote call.

### Optional settings

- `WRANGLER` selects the Wrangler command and defaults to `npx wrangler`.
- **`DEPLOY_WRANGLER_RETRY_BACKOFF_SECONDS`** - `2 5 15`: Space-separated
  backoff schedule for retrying a wrangler invocation whose output matches the
  transient-error allowlist. Items count = retry slots; total attempts =
  items + 1. Jitter of 0-1s is added per sleep when the base is >1s. Set to
  `0 0 0` in tests to keep wall time flat. When Cloudflare reports a version
  this run uploaded is missing (100146), reads and promotions use an extended
  default schedule with additional waits of 30 and 60 seconds. The deploy
  endpoint can still report the version missing after a successful read.
  An explicit retry schedule applies as supplied. Ordinary transient errors
  use the transient backoff and can leave submission uncertain. To qualify
  separately for a missing-version retry, the promotion requires
  Wrangler's API error on stderr to name the pinned version lookup and its
  missing-version code. With logging suppressed, or when deployment submission
  itself fails, a failed promotion retains its rollback obligation.
- **`DEPLOY_SCRIPT_RETRY_BACKOFF_SECONDS`** - `2 5 15`: Space-separated backoff
  schedule for `deploy_wrangler_retry` in `wrangler-retry.sh`, which the
  consumer's own preflight/postflight scripts call directly or through
  `deploy_d1_execute_json`. Same items-plus-one attempt arithmetic as the
  wrangler-retry schedule above, without the jitter - a deploy is serial, so
  there is no fleet to spread out. Unlike the schedules above it distinguishes
  unset from empty: set it to the empty string for no retries at all.
- **`DEPLOY_DIAGNOSTICS_DIR`** - (unset): If set, the pipeline writes
  `deploy-diagnostics.txt`, `wrangler-log-manifest.txt`, and a `wrangler-logs/`
  directory containing only logs newer than the deploy start marker. The path
  must be a dedicated artifact directory; if it already exists without the
  pipeline's ownership marker, the deploy aborts rather than wiping an unrelated
  path. Intended for CI failure artifacts and post-mortems.
- **`DEPLOY_VERSION_MESSAGE_ANNOTATION`** - (unset): Optional consumer-supplied
  token appended to every `versions upload --message`, making it part of each
  uploaded version's immutable annotations (readable later via
  `wrangler versions view`/`versions list` and the dashboard's version list).
  A consumer can stamp a rollback-compat generation (`gen=N`) here so any
  version's generation stays identifiable after the deploy.
- **`DEPLOY_COMMIT_SHA`** - (unset): The full lowercase commit being deployed,
  which `--sha` must abbreviate. Set, every upload's message names it
  (`commit=<sha>`), which is what `--expected-serving-sha` matches on a later
  deploy.
- **`DEPLOY_EXPECTED_RUNTIME`** - (unset): The Workers runtime the consumer
  expects its uploads to run at, as
  `{"compatibility_date": "YYYY-MM-DD", "compatibility_flags": [...]}` - for a
  consumer whose tests run at a configured runtime, that one. When set, the pipeline reads the uploaded
  version back with `versions view` before promotion, and refuses a version recorded at another runtime
  (`version_runtime.py`). A transient read failure is retried on the
  `DEPLOY_WRANGLER_RETRY_BACKOFF_SECONDS` schedule described above. A refusal names its cause:
  `RUNTIME MISMATCH`, or `RUNTIME NOT CHECKED` for an unreadable document or a
  failed read. Unset, nothing is read. Set to anything that is not that
  document, empty included, the pipeline refuses to start.

### Entry point

```sh
safe_deploy_pipeline.py <staging|production> [--sha SHA] [--expected-serving-sha SHA]
```

Runs the full sequence, including the exit path. The consumer wrapper passes
through `"$@"` from its own argv.

`--expected-serving-sha` is a full lowercase commit the caller expects to be
serving, typically the build it checked this deploy's delta against. Given it,
the capture that records the single serving version reads that version's upload
message (`wrangler versions view`) and aborts, before any mutation, unless it is
a final upload whose `commit=` is exactly this commit. A rollback from the
dashboard makes an older version serve, and that version's immutable message
still names the older commit. A version uploaded without `DEPLOY_COMMIT_SHA`
names no commit, and the capture aborts on it. A readable list with nothing
serving aborts too. It is an argument, not a variable, so that a caller running a
checkout whose pipeline predates it gets an unknown-argument refusal rather than
a deploy that ignored it.

## Constraints

- **The pipeline reserves no names in the wrapper's shell.** Its state lives on
  an object in its own process, so a wrapper's own `EXIT` trap and variables are
  its own business - and a hook cannot influence the recovery decision by
  assigning a variable.
- **Config must be exported.** The pipeline is a subprocess and reads its
  environment; an assigned-but-unexported variable is invisible to it, and a
  missing Wrangler environment or migrate command is refused at startup rather than
  mid-run.
- **Sets `CI=true` per wrangler call.** Every wrangler invocation runs with
  `CI=true` prefixed so wrangler suppresses its interactive "agent skills"
  banner, which prints to stdout in agent runtimes and would otherwise corrupt
  the machine-parsed wrangler output (the deployments and runtime JSON reads, and the `Worker Version ID:` grep off
  `versions upload`). CI mode is the only working suppression. The prefix is
  scoped to the wrangler process - the pipeline never exports `CI`, so consumer
  hooks keep the caller's own `$CI`.

## Testing the consumer wrapper

The toolkit's test harness drives a consumer's wrapper end-to-end with no real
wrangler. The pattern is to write a `.bats` driver that:

1. Sets `WRANGLER=$HARNESS_DIR/bin/mock-wrangler.sh` (the mock binary that
   classifies wrangler calls and replays committed fixtures).
2. Sets `WINDOWLESS_PREFLIGHT_CMD=true`, `MIGRATE_CMD=true` and `POSTFLIGHT_CMD=true` so the test doesn't need a
   real D1 connection or a real consumer migrate hook. The real wrapper should
   still wire postflight in strict mode (for example
   `check-schema-live.sh "$1" --require-schema`) so an empty DB under a
   required-schema check fails closed outside the test override path.
3. Has one `@test` per `MOCK_FAIL_AT=<step>` failure point, asserting on the
   mock's call log to verify the recovery layers fire at the right points.

See [`../test-harness/README.md`](../test-harness/README.md) for the
mock-wrangler step names, the slot UUID convention, and the full onboarding
walkthrough.

The pipeline has no per-step unit tests: the mock-driven end-to-end suite
exercises every step in context, and per-step unit tests would be an upside-down
test pyramid. What does get a suite of its own is a decision the end-to-end run
cannot reach - a document shape wrangler never sends in a test, a value the
operator supplies - which is what the `test_*.py` files beside the bats drivers
cover.

## Dependencies

- `python3` (the pipeline IS one, and its wrapper picks the interpreter.
  `wrangler-retry.sh` resolves its own when it is sourced, so a consumer's hook
  scripts fail at startup rather than mid-deploy on a host without one)
- POSIX `sh` (for a consumer's wrapper and its hook scripts; no bash-isms,
  tested with `dash` and `bash`)
- `jq` (spawned only where a consumer supplies a jq program rather than a
  value - the deployment verifier's SHA filter. Every other JSON read in the
  toolkit is `json.loads`, including the serving-version check in `deployments_list.py`)
- `wrangler` (`npx wrangler` by default; override via the `WRANGLER` env var)
- `git` (only for the default `--sha` derivation; can be skipped by passing
  `--sha` explicitly)
- `bats-core` (only for running the toolkit's own tests). The
  gates under `bin/` have their own run-time set: `python3` for
  `check_deploy_ref.py`, `require_github_actions.py`, and
  `verify_deployment.py`, `git` for the provenance gate, and `curl` plus `jq`
  for the deployment verifier, which reads its SHA filter through jq on the
  default `.sha` path as much as on a consumer's own. Install via
  `brew install bats-core jq python@3.14 grep` on Mac,
  `sudo apt install -y bats jq python3.14` on Debian/Ubuntu (plus a
  `ggrep` symlink to the system GNU `grep`), or your distro's equivalent.
  check-fixture-purity.sh additionally requires `ggrep`: it scans fixtures with
  a word-bounded pattern POSIX ERE does not define, and refuses to run rather
  than report a clean scan it never performed. macOS gets it from the `grep`
  formula; on Linux, symlink `ggrep` to the system GNU `grep`.
