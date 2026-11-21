# deploy-lib

The safe-deploy pipeline for a stateful Cloudflare Worker. The consumer's deploy
script becomes a ~30-line wrapper that supplies config and hooks; the pipeline
owns the full sequence and the concentric recovery path.

**The pipeline is a program the wrapper starts, not a library it sources.** The
consequences shape every wrapper: config is **exported**, because a subprocess
reads its environment, and hooks are **commands** run with the environment name
as their last argument.

## What's in this directory

The shell entry point a consumer's own scripts source:

- **`wrangler-retry.sh`**: The shell entry point to the roster below, plus
  `deploy_wrangler_retry` (any wrangler argv) and `deploy_d1_execute_json` (a D1
  flag-dispatch over it). A wrapper's hook scripts source it so their own
  Cloudflare reads retry against the same roster the pipeline classifies with.

The pipeline, which imports its neighbours by bare name:

- **`safe_deploy_pipeline.py`**: The entry point and the orchestration - the
  windowed, windowless and relocation bodies, the consumer hooks, and the
  wiring. Everything here owns no pipeline state of its own.
- **`deploy_config.py`**: What one run was asked to do, and every refusal that
  belongs before the first wrangler call.
- **`deploy_state.py`**: How far a deploy got, which is the whole input to the
  recovery decision, plus the abort that carries a run's exit status.
- **`deploy_versions.py`**: What the Worker is serving - the previous-version
  and current-version captures, the TOCTOU re-checks, the uploads, the promotes,
  the rollback, and `view_version`, the read of one version's document.
- **`deploy_database.py`**: What the deploy does to D1 - the lock and its
  read-backs, the drain, the bookmark, the unlock, and the restore.
- **`deploy_recovery.py`**: What runs on every exit: the recovery layers, the
  unlock obligation, and the diagnostics bundle.
- **`deploy_relocation.py`**: A relocation's source and target databases - which
  states of their deploy locks a run may start from, and who may reopen the
  source.
- **`deploy_reporting.py`**: Step groups, per-step timings, and the run's
  summary table.
- **`deploy_diagnostics.py`**: The failure bundle - the directory it may own,
  the wrangler logs this run produced, and the one-line-per-field record.
- **`deploy_lock.py`**: The pre-mutation, post-lock, and post-unlock reads and
  the record of each, on the retry loop that is deliberately broader than the
  transient roster.
- **`wrangler_runner.py`**: One wrangler call with bounded retries, and the
  run's retry count. Owns the decode and argument-validation ends of the
  encoding boundary: the surrogateescape decode that lets the retry classifier
  read whatever bytes the API sent, and the check that says whether a value
  carrying them can still be handed back as an argument.

Shared by the shell entry point and the pipeline:

- **`wrangler_transient.py`**: The transient-output roster. Owns which wrangler
  failures are worth another attempt, and why each shape is on the list; the
  pipeline's retries and the consumer's own reads classify through it.

The decisions the pipeline imports, each a pure function over one document or
value:

- **`deployments_list.py`**: The decisions over a
  `wrangler deployments list --json` document - which single version is serving,
  and whether the Worker has ever deployed at all. Owns every shape each one
  refuses and why refusing is the safe direction.
- **`version_message.py`**: The message an upload writes on a version, and the
  commit a version's message names. Owns why only a final upload's full commit
  identifies what is serving.
- **`version_runtime.py`**: The runtime a version document records and the one
  `DEPLOY_EXPECTED_RUNTIME` states. Owns why an omitted flags field is no flags
  and why a document it cannot read is not a mismatch.
- **`lock_state.py`**: What one `_deploy_lock` read means - locked, unlocked, or
  unreadable. Owns why the third answer is not a synonym for the second.
- **`diagnostics_path.py`**: Whether a value may be the diagnostics directory.
  Owns why a `.` or `..` component is refused even when the parent canonicalizes
  cleanly - cleanup runs `rm -rf` on the resolved path.
- **`retry_sleep.py`**: The wait for one attempt. Owns why the jitter exists,
  and why a zero base takes none.
- **`README.md`**: This file.

Which of these is spawned and which is only imported is decided by who reaches
it, not by the naming. `safe_deploy_pipeline.py` and `wrangler_transient.py` are
started as processes - by the consumer's wrapper and by `wrangler-retry.sh` -
and each carries a `main()`, a shebang, and the executable bit. Every other
module here is reached only by import and carries none of the three: a `main()`
on a module nothing spawns is an interface with no caller, and a suite pinning
its exit codes pins nothing a deploy depends on. The flat `snake_case` names are
what let one file be a process and an import at once, so a new module keeps that
shape - a hyphenated name cannot be imported by the test that covers it, and an
extensionless one is matched by no `*.py` lint or type-check glob, so it would
ship unlinted and unchecked.

## What the pipeline does

The pipeline runs `wrangler deploy` correctly - splitting it into script upload
-> version promotion -> schedules update so cron-limit failures abort cleanly
**before** anything goes live. It has windowed and windowless bodies, selected
by `DEPLOY_PIPELINE_MODE` (explicit config, never inferred from the change being
deployed; unset defaults to windowed, an unrecognized value aborts).

The **windowed** body (the default):

```
preflight check                <- consumer hook
capture previous version id    <- rollback target
upload maintenance version     <- versions upload --var MAINTENANCE_MODE:true
check its runtime              <- only with DEPLOY_EXPECTED_RUNTIME; before anything is live
update triggers                <- cron-limit failures land HERE
--- point of no return ---
promote maintenance version
lock DB -> drain -> bookmark
migrate                        <- consumer hook
postflight check               <- consumer hook
upload final version
promote final version
unlock DB                      <- via the exit path (success or failure)
```

The **windowless** body (`DEPLOY_PIPELINE_MODE=windowless`) takes no maintenance
window, no lock, no drain, and no bookmark - and therefore has **no D1
restore**; recovery is fix-forward. The consumer's preflight hook owns proving
the migration is safe to run under live traffic (additive/no-op under the old
serving code):

```
preflight check                <- consumer hook (owns windowless eligibility)
single-version preflight       <- exactly ONE version serving at 100%; its id
                                 is the rollback target AND the TOCTOU reference
update triggers                <- cron-limit failures still land here, before any DB mutation
re-verify serving version      <- TOCTOU re-check, immediately before the first DB mutation
migrate                        <- consumer hook (atomic imports; failure = fix forward)
upload final version
check its runtime              <- only with DEPLOY_EXPECTED_RUNTIME; old version still serving
re-verify serving version      <- TOCTOU re-check, immediately before promotion
promote final version          <- rollback if an attempt may have changed traffic
postflight check               <- consumer hook; failure after the promote is a
                                 loud red with NO automatic recovery
```

The **relocation** body (`DEPLOY_PIPELINE_MODE=relocation`) moves the Worker off
the database it is serving from (the source, `DEPLOY_RELOCATION_SOURCE_DB_*`)
onto the one this checkout deploys against (the target, `DEPLOY_DB_*`). The
Worker serves throughout, and what it refuses from the fence until the target
opens is the consumer's own lock contract: writes at the least, and every
request for a consumer that checks the lock on each. It takes no maintenance
version and no bookmark, and it never rolls the Worker back:

```
preflight check                <- consumer hook
read source and target locks   <- refuses any state a relocation may not start from
single-version preflight
update triggers
upload final version           <- bound to the target; not serving yet
check its runtime              <- only with DEPLOY_EXPECTED_RUNTIME; before the pause
--- the write pause starts, and its budget with it ---
fence the source               <- re-read, lock, read back; skipped on an acknowledged resume
drain                          <- DEPLOY_DRAIN_SECONDS, for requests admitted before the fence; a resume drains too
copy                           <- consumer hook (RELOCATE_COPY_CMD): must leave the
                                 target an exact, STILL-LOCKED copy, or fail
migrate                        <- consumer hook, against the target
read each lock back            <- a hook's exit status is not the state of a lock
re-verify serving version
--- point of no return ---
promote final version
verify it is what serves       <- read back, not inferred from the promote's status
open the target                <- unlock + read-back: the relocation is complete
postflight check               <- consumer hook
```

The deploy locks of the source and the target are its only durable record, which
is what lets a re-dispatch after a dead run go forward. A failure before the
promote reopens the source, if this run is the one that fenced it. A failure at
or after the promote never reopens the source and rolls nothing back. Until the
last step opens the target that leaves the source and the target locked, which
refuses every write and loses none; once the target is open the relocation is
complete, and a later failure - the postflight - is fixed forward with an
ordinary deploy. It never overwrites a target that holds data.
`deploy_relocation.py` owns the states and the reasons.

Cancellation ownership begins before a fenced hook is spawned. A termination
signal received during child creation is deferred until the parent has the
process handle, then stops and waits for the hook's process group before
redelivering the original signal. Repeated cancellation during cleanup cannot
interrupt that cleanup. This preserves the cancellation exit behavior and the
source fence while preventing an independently grouped copy from surviving its
pipeline, including when the child runs before its parent's spawn call returns.

An exit path runs on every exit, with concentric recovery layers:

- **`maint_live=false`, no `bookmark`**: Exit clean - old version still serving
- **...same, but a promote was in flight**: `wrangler rollback` to
  `prev_version_id` (worker only, never a restore). Either promote counts:
  `versions deploy <id>@100%` can apply server-side and still report failure, so
  a failed promotion is treated as possibly live unless every attempt recorded
  a rejection looking up the pinned version before submitting the deployment.
  Rollback to a pinned version is idempotent, so a promote that did not apply
  just re-pins the version already serving. This is the only rollback trigger on
  the windowless body, where `maint_live` is never set.
- **`maint_live=true`, no `bookmark`**: `wrangler rollback` to `prev_version_id`
- **`maint_live=true`, `bookmark` set**: D1 Time Travel restore +
  `wrangler rollback` + unlock

A failed recovery action does not stop the next one: the restore and the
rollback catch their own failure, so an operator sees every layer's outcome
rather than only the first that broke. The unlock then decides on those results.
It runs in every state except these, where the lock is left set on purpose: when
the final promote may have put real code live and the rollback did not take it
back down, because the lock is the last thing keeping writes off a Worker whose
serving version nobody can identify; and when the D1 restore failed, because the
migrate ran and was not undone, so the lock is what keeps any version's writes
out of a database that may be half-migrated. The GitHub Step Summary carries
that as a table on a best-effort basis - a summary file that cannot be written
is reported once and then left alone, and the recovery does not depend on it.

The lock/unlock path does not trust wrangler exit status as authoritative remote
state. The pipeline:

- reads `_deploy_lock` before the deploy mutates anything, refusing to start if
  the lock is already set or unreadable
- verifies `_deploy_lock` after lock and unlock attempts with bounded retries
  (`DEPLOY_VERIFY_BACKOFF_SECONDS`, default `1 2 4 8`)
- keeps `unlock required` separate from the last observed lock state, so an
  ambiguous failed lock still triggers an idempotent unlock attempt during
  cleanup
- treats only one non-zero read as a benign bootstrap state: `_deploy_lock`
  missing entirely before the first deploy
- can write a focused diagnostics bundle (`DEPLOY_DIAGNOSTICS_DIR`) with final
  recovery state plus only the Wrangler logs created during the current deploy
  run

Every wrangler invocation in the deploy pipeline - `deployments list`,
`versions upload` / `deploy`, `triggers deploy`, lock/unlock `d1 execute`,
`d1 time-travel info` / `restore`, and the recovery path's `rollback` - goes
through a bounded retry helper that classifies wrangler's error output against a
conservative allowlist. The one deliberate exception is the lock-state SELECT,
which is NOT routed through it because the lock read's own caller already
retries on any unreadable state, which is broader than the transient-allowlist
classifier (`deploy_lock.py`). The roster of transient shapes is
`wrangler_transient.py`, reached from shell as `deploy_output_is_transient` and
shared with the consumer's own scripts so an added shape covers every caller at
once. It matches wrangler's rendered text rather than a status the pipeline read
itself, which is why one Cloudflare status can need more than one entry: an
error the API returns as a JSON envelope renders as `HTTP <status>`, while one
the edge returns as an error page renders as a raw exchange dump carrying no
`HTTP` token at all. The schedule is configured via
`DEPLOY_WRANGLER_RETRY_BACKOFF_SECONDS` (default `2 5 15`, covering the initial
call and its configured retries). Unknown errors are NOT retried, so a missed
shape costs the deploy that meets it; the direction is still deliberate, because
the other way round (wrongly classifying an auth or validation error as
retryable) spends the whole schedule turning a clear verdict into a slow one.
Retry attempts are logged to stderr and the total retry count is recorded in the
diagnostics bundle as `total_wrangler_retries` so operators can spot creeping
retry rates.

Serialize deploys to each environment and leave its traffic split and database
locks under pipeline control through recovery. On a windowless visibility
retry the pipeline checks the serving version again before sending the pinned
promotion. During relocation the source is already fenced when promotion
starts. A missing target there enters the usual forward recovery immediately,
so waiting for propagation cannot extend the write pause.

The wrangler retry loop layers beneath the lock read-back verify: a retry on the
lock UPDATE may succeed on the second attempt, but if it does not, the read-back
check remains authoritative for the recovery decision (the split-brain case
where the UPDATE applied server-side but wrangler reported failure still
resolves correctly).

One thing the pipeline records that a shell caller could not: the per-read lock
fields (`last_lock_*`, and the `_attempts` and `_error` cells of each phase) and
the "Lock-state verification error while trying to ..." line. A shell reading
its lock inside a command substitution writes those in a subshell and loses
them; the pipeline holds the record on an object. This is the diagnostics bundle
rather than the deploy - nothing about which deploys proceed depends on it.

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
export DEPLOY_DB_STAGING="my-db-staging"
export DEPLOY_DB_PRODUCTION="my-db"
export DEPLOY_WRANGLER_ENV_STAGING="staging"
export DEPLOY_WRANGLER_ENV_PRODUCTION="production"

# === hook commands (pipeline appends $env as the last argument) ===
export PREFLIGHT_CMD="$SCRIPT_DIR/check-schema-live.sh --require-schema"
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
- **Hooks are commands.** Each is run with the environment name appended as its
  last argument, so a command may carry its own leading arguments (the example
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

### Required config (exported, so the pipeline's process reads it)

- **`DEPLOY_DB_STAGING`**: D1 database name for staging
- **`DEPLOY_DB_PRODUCTION`**: D1 database name for production
- **`DEPLOY_WRANGLER_ENV_STAGING`**: wrangler env name for staging (usually
  `staging`)
- **`DEPLOY_WRANGLER_ENV_PRODUCTION`**: wrangler env name for production
  (usually `production`)

### Hooks

The pipeline runs the commands the consumer names in the environment -
`PREFLIGHT_CMD`, `MIGRATE_CMD`, `POSTFLIGHT_CMD`, and on the relocation body
`RELOCATE_COPY_CMD` - each with the environment name appended as its last
argument:

- **`PREFLIGHT_CMD`** - Optional: Before any wrangler operations in the windowed
  and windowless bodies. Failure aborts before anything goes live. On the
  windowless body this hook owns eligibility: it must refuse any change that is
  not safe to migrate under live traffic.
- **`MIGRATE_CMD`** - **Required**: Windowed: after lock+bookmark, before final
  upload. Windowless: after the pre-mutation TOCTOU re-check, with NO
  lock/bookmark held - a failure aborts fix-forward (old version serving, no
  restore). An unset `MIGRATE_CMD` is refused at startup, before the maintenance
  window opens.
- **`POSTFLIGHT_CMD`** - Optional: Windowed: after migrate, before final
  upload - failure triggers full concentric recovery. Windowless: after the
  final promotion - failure is a loud red with no automatic recovery (fix
  forward). When this hook verifies required schema presence, it must reject an
  empty DB (for example `check-schema-live.sh "$1" --require-schema`).

- **`RELOCATE_COPY_CMD`** - **Required on the relocation body**, refused at
  startup when unset: runs once the source is fenced. It must leave the target
  an exact copy of the source with the target's deploy lock still set, or exit
  non-zero. When the target already holds a copy - a resumed relocation - it
  must prove that copy still exact and never overwrite it. The pipeline opens
  the target; the hook never does.

Each hook receives the env name (`staging` or `production`) as its last
argument. Hooks exit or `return` non-zero to fail the deploy.

### Optional env var overrides

- **`DEPLOY_PIPELINE_MODE`** - `windowed`: Which body runs: `windowed`
  (maintenance window, every protection), `windowless` (no window; fix-forward
  recovery) or `relocation` (moves the Worker onto another database). Any other
  value aborts before the preflight. Consumers that route deploys (e.g. on a
  schema-safety verdict) set this explicitly per run.
- **`DEPLOY_RELOCATION_SOURCE_DB_STAGING`** /
  **`DEPLOY_RELOCATION_SOURCE_DB_PRODUCTION`** - (unset): The relocation body's
  source database, by name. Required on that body and refused when it names the
  target. The name need not be in the checkout's wrangler config, which binds
  the target: wrangler resolves it against the account.
- **`DEPLOY_RELOCATION_MAX_PAUSE_SECONDS`** - `240`: How long a relocation may
  hold its source fenced before it must have reached the promote. The copy and
  migrate hooks run against that deadline: one that runs past it is stopped,
  with every process it started, and the run backs out. It is checked once more
  immediately before the promote, which is the last point at which running long
  can still be answered by reopening the source. It bounds the time to the
  promote, not the pause: writes resume later than the deadline on either way
  out, so set it below the consumer's ceiling on the whole pause by the longer
  of them. On the way through, that is the promote, the serving check and
  opening the target. On the way back, it is the stop sequence - up to its grace
  period (`HOOK_STOP_GRACE_SECONDS`) before the hook's group is killed - and
  then reopening the source, with its read-back. Refused below `1`.
- **`DEPLOY_RELOCATION_RESUME`** - (unset): Set to `true` to continue a
  relocation whose source an earlier run already fenced. A set lock cannot say
  who set it - a deploy that died inside its maintenance window leaves the same
  bit - so a fenced source is refused without this, and this is refused when the
  source turns out to be open.
- **`WRANGLER`** - `npx wrangler`: Path to wrangler binary. The
  cloudflare-deploy-toolkit harness sets this to `bin/mock-wrangler.sh` for
  tests.
- **`DEPLOY_DRAIN_SECONDS`** - `15`: How long to wait for in-flight requests to
  drain after the database is locked: on the windowed body before the bookmark,
  on the relocation body before the copy, where it counts against
  `DEPLOY_RELOCATION_MAX_PAUSE_SECONDS`. Set this above the consumer's enforced
  worst-case runtime for anything that writes the database - requests,
  scheduled invocations and `waitUntil` tasks - or guard each write in its own
  SQL; `worker-patterns/README.md` owns that checklist.
- **`DEPLOY_VERIFY_BACKOFF_SECONDS`** - `1 2 4 8`: Space-separated backoff
  schedule for `_deploy_lock` read-back verification after preflight/lock/unlock
  reads.
- **`DEPLOY_WRANGLER_RETRY_BACKOFF_SECONDS`** - `2 5 15`: Space-separated
  backoff schedule for retrying a wrangler invocation whose output matches the
  transient-error allowlist. Items count = retry slots; total attempts =
  items + 1. Jitter of 0-1s is added per sleep when the base is >1s. Set to
  `0 0 0` in tests to keep wall time flat. When Cloudflare reports a version
  this run uploaded is missing (100146), reads and windowed or windowless
  promotions use an extended
  default schedule with additional waits of 30 and 60 seconds. The deploy
  endpoint can still report the version missing after a successful read.
  An explicit retry schedule applies as supplied. Promotion retries require
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
  consumer whose tests run at a configured runtime, that one. Set, each body
  reads its first upload back with `versions view` before any window or write
  pause opens, and refuses a version recorded at another runtime
  (`version_runtime.py`). A transient read failure is retried on the
  `DEPLOY_WRANGLER_RETRY_BACKOFF_SECONDS` schedule described above. A refusal names its cause:
  `RUNTIME MISMATCH`, or `RUNTIME NOT CHECKED` for an unreadable document or a
  failed read. Unset, nothing is read. Set to anything that is not that
  document, empty included, the pipeline refuses to start.

The pipeline reads `DEPLOY_DRAIN_SECONDS` and the lock-verification and
wrangler-retry backoff schedules when it starts and refuses anything that is not
a non-negative whole number, since those are the values it can actually wait.
Refusing at startup rather than at the wait matters because the windowed body
does not reach its first wait until the maintenance version is already serving.

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
  missing database name or migrate command is refused at startup rather than
  mid-run.
- **`_deploy_lock` table name is hardcoded.** This matches the
  `worker-patterns/deploy-lock.sql` convention. If you need a different table
  name, fork `deploy-lib/` for now; making this configurable is a non-breaking
  change later.
- **D1-shaped.** The lock + bookmark + Time Travel restore steps assume D1 as
  the database. A KV-only or Hyperdrive-only Worker would still benefit from the
  upload/triggers/promote split but would not exercise the lock/drain/bookmark
  layers; that variant is a follow-up if a consumer asks for it.
- **Sets `CI=true` per wrangler call.** Every wrangler invocation runs with
  `CI=true` prefixed so wrangler suppresses its interactive "agent skills"
  banner, which prints to stdout in agent runtimes and would otherwise corrupt
  the machine-parsed wrangler output (the `--json` lock, deployments, and Time
  Travel reads, and the `Worker Version ID:` grep off
  `versions upload`). CI mode is the only working suppression. The prefix is
  scoped to the wrangler process - the pipeline never exports `CI`, so consumer
  hooks keep the caller's own `$CI`.

## Testing the consumer wrapper

The toolkit's test harness drives a consumer's wrapper end-to-end with no real
wrangler. The pattern is to write a `.bats` driver that:

1. Sets `WRANGLER=$HARNESS_DIR/bin/mock-wrangler.sh` (the mock binary that
   classifies wrangler calls and replays committed fixtures).
2. Sets `MIGRATE_CMD=true` and `POSTFLIGHT_CMD=true` so the test doesn't need a
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
  toolkit is `json.loads`, and the ones that carry a decision are
  `deployments_list.py` and `lock_state.py`)
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

## Why this is a library, not a copy-paste script

The toolkit's first consumer had a 525-line hand-rolled `deploy-safe.sh`. As
soon as a second stateful Cloudflare Worker showed up on the roadmap,
copy-pasting 525 lines of orchestration into a new project was clearly the wrong
call. Lifting the generic parts into a library means a new consumer can write
its wrapper in ~30 lines of boilerplate (plus a few lines per hook and whatever
routing/authorization policy the consumer layers on) while still getting the
entire concentric recovery story.
