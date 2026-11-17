# cloudflare-deploy-toolkit

Safely deploy stateful Cloudflare Workers - gradual-deployments split,
lock/drain/bookmark, and concentric failure recovery - without writing 600 lines
of POSIX `sh` yourself.

## What it is

A deploy pipeline, a set of deploy gates, a test harness, Worker code patterns
and GitHub Actions examples for any wrangler-based Worker with state (D1, KV,
Durable Objects) that needs to keep working after a deploy fails mid-flight:

1. **A deploy pipeline** (`deploy-lib/`) that runs the gradual-deployments split
   (`versions upload` -> `triggers deploy` -> `versions deploy <id>@100%`), the
   lock/drain/bookmark sequence, verified `_deploy_lock` read-back before and
   after lock mutations, a focused diagnostics bundle for failure artifacts, and
   the concentric recovery path. A consumer reaches it through
   `safe_deploy_pipeline.py`, run by a wrapper that exports config and hook
   commands. The wrapper is boilerplate - ~30 lines plus a few per hook, before
   whatever routing or authorization policy a consumer layers on top.
2. **A shared deployment-verification helper** (`bin/verify_deployment.py`) that
   checks the latest Cloudflare deployment timestamp with one Linux/macOS-stable
   RFC3339 parser and the same bounded future-skew / max-age rules used by the
   reference workflows. A timestamp carrying no UTC offset is refused rather
   than assumed, because a reader that guesses the zone is the bug this helper
   exists for. Cloudflare's deployments list can lag a deploy that just landed,
   so a stale answer is read again for about 25 seconds before the helper
   fails. It optionally polls a Worker URL that echoes the running build's
   commit SHA (`VERIFY_DEPLOYMENT_EXPECTED_SHA` + `VERIFY_DEPLOYMENT_SHA_URL`)
   and fails closed unless the live build matches the expected commit - the
   timestamp alone passes for a sibling deploy that landed between promote and
   verify.
3. **A test harness** for the deploy pipeline and the gates: a mock wrangler
   binary, fixture replay, named-step failure injection, a fixture-purity scan,
   and a Python suite per `bin/` gate driving its real executable - the shapes
   split by what they drive, since a gate's subject is a git repo or an API
   response rather than a wrangler fixture flow.
4. **Worker code patterns** for the in-Worker side of safe deploys: the
   maintenance-mode gate, the deploy-lock table, and the `assertNotLocked()`
   helper called once at the router (see `worker-patterns/README.md` for the
   bounded-in-flight drain gate rationale).
5. **Workflow examples** - copy-pasteable GitHub Actions YAML for the deploy +
   verify pipeline and the manual unlock workflow.

## Why it exists

`wrangler deploy` is one CLI command but covers script upload, version
promotion, and schedules update under the hood:

1. script upload
2. version promotion
3. schedules update

If any step after the first fails, the worker is already promoted live with
whatever bindings the upload step had - including any transient
`MAINTENANCE_MODE=true` env var meant to be torn down a few seconds later. The
standard `wrangler deploy` failure handling does not unwind the partial
promotion.

Without recovery layers that distinguish "promoted but not yet locked" from
"promoted and locked," the standard cleanup path leaves the worker live with
those transient bindings.

The fix is the gradual-deployments split (`wrangler versions upload` ->
`wrangler triggers deploy` -> `wrangler versions deploy <id>@100%`) plus a
concentric recovery path that handles the cron-limit-error / DB-lock-failure /
migration-failure scenarios with worker rollback + D1 Time Travel restore. Lock
and unlock cleanup use `_deploy_lock` read-back as the source of truth, so a
wrangler timeout or 429 cannot be misread as "the mutation definitely did not
happen." As soon as a second stateful Worker showed up on the roadmap,
copy-pasting 600+ lines of POSIX `sh` into a new project was clearly the wrong
call - hence the toolkit.

The deploy pipeline is genuinely complex (mutable run state, an exit path,
recovery layers), so testing it well is the difference between "this incident
never happens again" and "we hope it never happens again." The toolkit's main
job is to make that test cheap.

A search for prior art across npm, the GitHub Actions marketplace, and
Cloudflare's own docs found nothing equivalent.

## How to use it

### Requirements

- Python 3.14 or newer; the code uses 3.14 syntax. The example wrapper and
  `deploy-lib/wrangler-retry.sh` try `python3.14` before a bare `python3`. The
  `bin/` gates run through their `python3` shebang, so either put a 3.14
  `python3` first on PATH or invoke them as `python3.14 bin/<gate>.py`. A
  GitHub-hosted runner gets one from `actions/setup-python`.
- POSIX `sh` for the shell pieces; `git` for the provenance gate; `curl` and
  `jq` for the deployment verifier; `wrangler` for the pipeline itself.
- To run the toolkit's own tests: `bats`, `shellcheck`, `jq`, a current `bash`,
  and GNU grep on PATH as `ggrep` (`test-harness/README.md`).

### Install

The toolkit is a deploy pipeline under `deploy-lib/`, a set of Python gates
under `bin/`, and a handful of code patterns and YAML examples. There is no
compile or install step - your deploy script runs the pipeline entry point, and
the gates are invoked as commands. What each gate needs on PATH beyond a Python
3.14 interpreter is what it spawns: `git` for the provenance gate, `curl` and `jq`
for the deployment verifier, nothing for the CI-execution gate.

Options for getting the toolkit into your repo:

#### 1. Git submodule (recommended)

Pin to a tag for reproducibility:

```sh
git submodule add https://github.com/curlewlabs-com/cloudflare-deploy-toolkit \
  vendor/cloudflare-deploy-toolkit
git -C vendor/cloudflare-deploy-toolkit checkout v0.1.0   # or whatever tag
git submodule update --init --recursive
```

Then in your wrapper script:

```sh
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DEPLOY_TOOLKIT_DIR="$SCRIPT_DIR/../vendor/cloudflare-deploy-toolkit"
exec python3.14 "$DEPLOY_TOOLKIT_DIR/deploy-lib/safe_deploy_pipeline.py" "$@"
```

A wrapper whose own hook scripts talk to Cloudflare sources
`deploy-lib/wrangler-retry.sh` as well, so those reads retry against the same
roster; `DEPLOY_TOOLKIT_DIR` is how that lib finds the roster.

#### 2. Vendor copy

Copy the whole of `deploy-lib/` for the deploy wrapper - the pipeline refuses to
load without the modules beside it, which it imports by bare name - plus
`bin/verify_deployment.py` if you want the shared post-deploy verify step, and
any patterns you need from `worker-patterns/` directly into your repo. If you
also copy the workflow examples, keep the helper path layout consistent or
update the YAML to match your vendored location. You'll need to track upstream
manually for fixes.

#### 3. Release archive

Each release carries `cloudflare-deploy-toolkit-<tag>.tar.gz`, the tagged tree
under a `cloudflare-deploy-toolkit-<tag>/` prefix, and a `SHA256SUMS` file
beside it. Releases are immutable, so the archive's digest never changes once
published; pin the URL and the digest, and verify before extracting:

```sh
TAG=v0.1.0
URL=https://github.com/curlewlabs-com/cloudflare-deploy-toolkit/releases/download/$TAG/cloudflare-deploy-toolkit-$TAG.tar.gz
curl -fsSLo toolkit.tar.gz "$URL"
echo "<sha256 from SHA256SUMS>  toolkit.tar.gz" | shasum -a 256 -c -
tar -xzf toolkit.tar.gz -C vendor
```

#### 4. Single-file curl

For automated tooling that wants the latest pipeline and verify helper:

```sh
BASE=https://raw.githubusercontent.com/curlewlabs-com/cloudflare-deploy-toolkit/v0.1.0
DEST=vendor/cloudflare-deploy-toolkit
mkdir -p "$DEST/deploy-lib" "$DEST/bin"
for f in deploy-lib/wrangler-retry.sh \
    deploy-lib/safe_deploy_pipeline.py deploy-lib/deploy_config.py \
    deploy-lib/deploy_state.py deploy-lib/deploy_reporting.py \
    deploy-lib/deploy_versions.py deploy-lib/deploy_database.py \
    deploy-lib/deploy_recovery.py deploy-lib/deploy_relocation.py \
    deploy-lib/deploy_diagnostics.py \
    deploy-lib/deploy_lock.py deploy-lib/wrangler_runner.py \
    deploy-lib/wrangler_transient.py deploy-lib/deployments_list.py \
    deploy-lib/lock_state.py deploy-lib/diagnostics_path.py \
    deploy-lib/retry_sleep.py deploy-lib/version_message.py \
    deploy-lib/version_runtime.py bin/verify_deployment.py; do
  curl -fsSL "$BASE/$f" > "$DEST/$f"
done
```

Always pin to a specific tag rather than `main`.

The tools depend on the directory layout, which is why this fetches into a tree
rather than flattening into one directory: `wrangler-retry.sh` resolves its
roster under the `DEPLOY_TOOLKIT_DIR` your wrapper exports, the Python modules
import each other by bare name from the directory they share, and each refuses
to load without the files it needs. The fetch list above is the roster; this
paragraph does not repeat it. A flat copy, or a roster short one file, fails at
startup rather than mid-deploy.

Once installed, write a wrapper: it exports the database and
wrangler-environment config variables plus its hook commands and runs
`safe_deploy_pipeline.py`. See [`deploy-lib/README.md`](deploy-lib/README.md)
for the full API.

## Layout

```
cloudflare-deploy-toolkit/
+-- LICENSE                       MIT
+-- README.md                     (you are here)
+-- bin/                          gates a consumer invokes as a command
|   +-- check_deploy_ref.py       deploy provenance: first-parent origin/main, clean tree
|   +-- require_github_actions.py CI-execution gate with the ALLOW_LOCAL_DEPLOY break-glass
|   +-- verify_deployment.py      shared deployment verifier: timestamp + optional live-SHA check
+-- deploy-lib/                   the deploy pipeline
|   +-- README.md                 API, hook contract, env var config, constraints,
|   |                             and what every other file here owns
+-- test-harness/                 test-time tooling for the deploy pipeline
|   +-- README.md                 architecture: layers, fixture format, slot UUIDs
|   +-- bin/
|   |   +-- sanitize-wrangler-text.sh    PII scrub for fixture capture
|   |   +-- mock-wrangler.sh             fake wrangler binary, replays fixtures
|   |   +-- check-fixture-purity.sh      scans committed fixtures for leaks
|   +-- lib/
|   |   +-- test-slots.sh                shared slot UUID constants
|   |   +-- capture-helpers.sh           sourceable wrappers for wrangler calls
|   +-- test/
|       +-- test-*.bats, test_*.py             the suites; test-harness/README.md sec. Test layers names the risk each owns
|       +-- example-deploy-wrapper.sh          minimal example wrapper; what the end-to-end suites drive
|       +-- example-hook.sh                    the hook command the example wrapper points at
|       +-- example-fixtures/                  generic mock-wrangler fixtures for the end-to-end suites
|       +-- check.sh                           runs shellcheck, bats, the Python suites, and the vendored-path smoke from CI
+-- worker-patterns/              TS code patterns for Worker code
|   +-- README.md                 when and how to use each pattern
|   +-- maintenance-gate.ts       request-time MAINTENANCE_MODE check (returns 503)
|   +-- deploy-lock.ts            assertNotLocked() helper, called once at the router
|   +-- deploy-lock.sql           _deploy_lock table DDL
+-- workflow-examples/            sample GitHub Actions YAML
    +-- README.md                 how to adapt the examples for a new Worker
    +-- deploy.yml                generalized deploy workflow with safe pipeline
    +-- unlock-db.yml             generalized manual unlock workflow
```

## Onboarding a new Worker

Read the per-area READMEs in this order:

- **For your deploy script:** start at
  [`deploy-lib/README.md`](deploy-lib/README.md). You'll write a wrapper that
  supplies config and the hooks and hands off to the pipeline entry point - the
  pipeline does the rest.
- **For your Worker code:**
  [`worker-patterns/README.md`](worker-patterns/README.md). The patterns are
  copy-paste-and-adapt - they assume D1 + a `_deploy_lock` table.
- **For testing your deploy script:**
  [`test-harness/README.md`](test-harness/README.md). You'll write a small
  `.bats` test driver that points at `bin/mock-wrangler.sh`, plus a capture
  script that calls `lib/capture-helpers.sh`.
- **For your CI workflow:**
  [`workflow-examples/README.md`](workflow-examples/README.md). The examples are
  working YAML with the consumer-specific bits flagged for replacement, and they
  rely on `bin/verify_deployment.py` for the shared post-deploy verify step.

## Constraint: introducing a new Durable Object class

`wrangler versions upload` (used by the maintenance and final phases of the
safe-deploy pipeline) cannot apply _new_ Durable Object migrations. Per
Cloudflare's docs:

- [Versions & Deployments](https://developers.cloudflare.com/workers/configuration/versions-and-deployments/):
  "Uploading a version with Durable Object migrations is not supported. Use
  wrangler deploy if you are applying a Durable Object migration."
- [Gradual Deployments sec. migrations](https://developers.cloudflare.com/workers/configuration/versions-and-deployments/gradual-deployments/#migrations):
  "Versions of Worker bundles containing new Durable Object migrations cannot be
  uploaded."

Already-applied migration tags in `wrangler.toml` are fine - `versions upload`
accepts them and they document the migration record. The constraint is on adding
a _new_ migration tag (or a new `new_sqlite_classes` / `new_classes` entry on an
existing tag).

**To introduce a new Durable Object class** (steps written for `wrangler.toml`;
JSON/JSONC consumers do the equivalent in their format):

1. Add the binding to `wrangler.toml` under `[[durable_objects.bindings]]` and
   under each `[env.X]` block - wrangler does not inherit `durable_objects` into
   named envs.
2. Add the `[[migrations]]` block with the appropriate tag and
   `new_sqlite_classes` (or `new_classes` for KV-backed) entry.
3. Run a one-shot `wrangler deploy --env <env>` per environment to apply the
   migration. Do **not** run the safe-deploy pipeline yet - its
   `versions upload` calls will reject a new migration.
4. Once the migration has been applied in every environment, leave the binding
   and the `[[migrations]]` block in place. The block now documents an
   already-applied migration; subsequent deploys go through the safe pipeline
   normally.

Step 3 is a bare `wrangler` call, so it bypasses whatever wrapper the consumer
normally deploys through, and everything that wrapper does for you has to be
done by hand. The omissions are silent at the time and surface later:

- **A prebuilt `main`.** A config using `no_bundle` with `main` pointing into a
  build directory ships whatever that directory already holds - wrangler does no
  bundling, and its `--define` is a no-op there. Run the consumer's build step
  first, or the one-shot deploys a stale artifact. Where the consumer bakes
  deploy-gate state into that artifact, a stale one is worse than out of date:
  a consumer that bakes a rollback-compat generation in and has its preflight
  read it back off `/health` would see the environment report the generation of
  whatever was built last.
- **Version annotations, where the consumer's preflight reads them.** The
  pipeline appends `DEPLOY_VERSION_MESSAGE_ANNOTATION` to its upload message
  (`deploy-lib/README.md`); a bare `wrangler deploy` appends nothing, so pass
  the same text in `--message`. For a consumer whose gate reads that message
  back, the cost lands on the _next_ pipeline deploy rather than on the
  one-shot, which succeeds either way.

The same procedure applies on a fresh-account migration. The pipeline does not
preflight-check for migration blocks - `wrangler versions upload` surfaces the
new-migration constraint clearly enough at deploy time ("Uploading a version
with Durable Object migrations is not supported") that an additional guard would
only over-reject already-applied tags.

## Limits

- **A Worker's first deploy is a plain `wrangler deploy`.** The pipeline reads
  the serving version before it changes anything, as its rollback target, and
  refuses a deployments list it cannot read rather than treat it as a first
  deploy. A Worker that has never been deployed has no list to read, so its
  first version goes out once with `wrangler deploy`; every deploy after that
  goes through the pipeline. The same holds for a new Durable Object class, as
  [the constraint above](#constraint-introducing-a-new-durable-object-class)
  describes.
- **A relocation's pause budget bounds its hooks, not the pipeline's own
  wrangler calls.** `DEPLOY_RELOCATION_MAX_PAUSE_SECONDS` stops a copy or
  migrate hook that runs past it, and is checked between steps. A wrangler
  call the pipeline makes inside the pause (a lock write or read-back, the
  serving-version check) is not cut off, because a lock write cut off on the
  client can still land on Cloudflare after recovery has read the lock, leaving
  a lock nobody owns. Such a call ends when wrangler gives up, which stretches
  the pause; the locks stay set throughout, so no write is lost.

## Versioning

Releases are fixed `vMAJOR.MINOR.PATCH` tags with an immutable GitHub Release.
While the version is 0.x, a minor release may change the wrapper contract, the
hook contract or the configuration variables; read the release notes before
moving a pin. Pin a consumer to a tag or a commit, never to `main`. How a
release is cut is in [CONTRIBUTING.md](CONTRIBUTING.md#releasing).

## Running the tests

```sh
./test-harness/test/check.sh
```

It runs shellcheck over every shell script, the Bats suites, the Python suites,
and the same run again from a vendored copy of the tree.

After a bump of CI's shellcheck or bats version,
`python3 tools/refresh_test_tools.py` rewrites the digests CI checks those
archives against.

## License

MIT - see [LICENSE](LICENSE).
