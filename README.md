# cloudflare-deploy-toolkit

Deploy compatible updates to a stateful Cloudflare Worker while it serves
traffic. The pipeline checks the serving version before applying a migration
and again before promoting the new build.

## What it is

1. **A windowless deploy pipeline** captures the single serving
   version, runs your preflight against it, updates triggers, applies your compatible migration, uploads
   the final version and checks its runtime when configured. It promotes the
   version and runs your postflight. An ambiguous promotion failure triggers
   Worker rollback to the captured version. Migration and postflight failures
   require investigation and a forward fix.
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
3. **A workflow example** wires a deployment and its verification into GitHub
   Actions. The provenance and CI-execution gates can also run independently.

## Why it exists

A trigger update can fail after a plain Wrangler deploy has already changed
the live version. Separating trigger sync, schema migration, upload and
promotion lets the pipeline handle each failure according to what might have
changed. Consumer migrations must work with the old Worker still serving.

## How to use it

### Requirements

- Python 3.14 or newer; the code uses 3.14 syntax. The example wrapper and
  `deploy-lib/wrangler-retry.sh` try `python3.14` before a bare `python3`. The
  `bin/` gates run through their `python3` shebang, so either put a 3.14
  `python3` first on PATH or invoke them as `python3.14 bin/<gate>.py`. A
  GitHub-hosted runner gets one from `actions/setup-python`.
- POSIX `sh` for the shell pieces; `git` for the provenance gate; `curl` and
  `jq` for the deployment verifier; `wrangler` for the pipeline itself.

### Install

The toolkit is a deploy pipeline under `deploy-lib/`, a set of Python gates
under `bin/`, and a GitHub Actions example. There is no
compile or install step - your deploy script runs the pipeline entry point, and
the gates are invoked as commands. What each gate needs on PATH beyond a Python
3.14 interpreter is what it spawns: `git` for the provenance gate, `curl` and `jq`
for the deployment verifier, nothing for the CI-execution gate.

#### Release archive

Each release carries `cloudflare-deploy-toolkit-<tag>.tar.gz`, the tagged tree
under a `cloudflare-deploy-toolkit-<tag>/` prefix. Pin the archive URL and the
reviewed digest from the release's `SHA256SUMS`. Keep the tag and digest in
the consumer's checkout alongside the matching wrapper. Run this setup from
that checkout's root with `TOOLKIT_TAG` and `TOOLKIT_SHA256` set from its pin.

```sh
set -eu
TAG=${TOOLKIT_TAG:?Set TOOLKIT_TAG to a published release tag}
SHA256=${TOOLKIT_SHA256:?Set TOOLKIT_SHA256 to the pinned archive digest}
URL=https://github.com/curlewlabs-com/cloudflare-deploy-toolkit/releases/download/$TAG/cloudflare-deploy-toolkit-$TAG.tar.gz
INSTALL_DIR=$(mktemp -d "${RUNNER_TEMP:-${TMPDIR:-/tmp}}/toolkit.XXXXXXXX")
ARCHIVE="$INSTALL_DIR/toolkit.tar.gz"
curl -fsSLo "$ARCHIVE" --retry 3 --retry-connrefused "$URL"
printf '%s  %s\n' "$SHA256" "$ARCHIVE" | shasum -a 256 -c -
tar -xzf "$ARCHIVE" -C "$INSTALL_DIR"
TOOLKIT_DIR=$(cd "$INSTALL_DIR/cloudflare-deploy-toolkit-$TAG" && pwd)
if [ -n "${GITHUB_ENV:-}" ]; then
  printf 'DEPLOY_TOOLKIT_DIR=%s\n' "$TOOLKIT_DIR" >> "$GITHUB_ENV"
else
  printf '%s\n' "$TOOLKIT_DIR"
fi
```

In GitHub Actions, run this as its own setup step after checkout and before
deploying. It sets `DEPLOY_TOOLKIT_DIR` for later steps after verifying and
extracting the archive. The temporary directory keeps the checkout clean for
the provenance gate. For a local invocation, set `DEPLOY_TOOLKIT_DIR` to the
path printed by the setup. See the [workflow setup](workflow-examples/README.md).

#### Git submodule

A submodule records the exact commit in the consuming repository. Review that
commit and its matching pipeline guide before committing the submodule:

```sh
git submodule add https://github.com/curlewlabs-com/cloudflare-deploy-toolkit \
  vendor/cloudflare-deploy-toolkit
git -C vendor/cloudflare-deploy-toolkit rev-parse HEAD
git submodule update --init --recursive
```

Then in your wrapper script:

```sh
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
export DEPLOY_TOOLKIT_DIR="${DEPLOY_TOOLKIT_DIR:-$SCRIPT_DIR/../vendor/cloudflare-deploy-toolkit}"
exec python3.14 "$DEPLOY_TOOLKIT_DIR/deploy-lib/safe_deploy_pipeline.py" "$@"
```

A wrapper whose own hook scripts talk to Cloudflare sources
`deploy-lib/wrangler-retry.sh` as well, so those reads retry against the same
roster; `DEPLOY_TOOLKIT_DIR` is how that lib finds the roster.

Keep the installed tree together. The pipeline loads its Python modules from
`deploy-lib/`, and `wrangler-retry.sh` uses `DEPLOY_TOOLKIT_DIR` to find the retry
classifier.

Once installed, write a wrapper: it exports the
Wrangler environment config variables plus its hook commands and runs
`safe_deploy_pipeline.py`. See [`deploy-lib/README.md`](deploy-lib/README.md)
for the full API.

## Layout

```
cloudflare-deploy-toolkit/
+-- LICENSE-APACHE                Apache-2.0
+-- LICENSE-MIT                   MIT
+-- README.md                     (you are here)
+-- bin/                          gates a consumer invokes as a command
|   +-- check_deploy_ref.py       deploy provenance: first-parent origin/main, clean tree
|   +-- require_github_actions.py CI-execution gate with the ALLOW_LOCAL_DEPLOY break-glass
|   +-- verify_deployment.py      shared deployment verifier: timestamp + optional live-SHA check
+-- deploy-lib/                   the deploy pipeline
|   +-- README.md                 API, hook contract, env var config, constraints,
|   |                             and what every other file here owns
+-- test-harness/                 repository tests and fixtures
+-- workflow-examples/            sample GitHub Actions YAML
    +-- README.md                 how to adapt the examples for a new Worker
    +-- deploy.yml                generalized deploy workflow with safe pipeline
```

## Onboarding a new Worker

Read the per-area READMEs in this order:

- **For your deploy script:** start at
  [`deploy-lib/README.md`](deploy-lib/README.md). You'll write a wrapper that
  supplies config and the hooks and hands off to the pipeline entry point - the
  pipeline does the rest.
- **For your CI workflow:**
  [`workflow-examples/README.md`](workflow-examples/README.md). The examples are
  working YAML with the consumer-specific bits flagged for replacement, and they
  rely on `bin/verify_deployment.py` for the shared post-deploy verify step.

## Constraint: introducing a new Durable Object class

`wrangler versions upload` (used by the
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
- Database cutovers require a separate recovery procedure. The consumer
  preflight must verify the candidate binding and the migration hook target
  both resolve to the database bound to the captured Worker version.
- Use expand/contract migrations that keep the serving and rollback versions
  compatible through every database state that a partially applied migration can
  leave behind. Wait for propagation and outstanding requests from older versions
  to finish before contracting a schema they still use. Even a code-only change can write data before rollback. Verify
  both versions can consume each other's writes throughout recovery. The
  toolkit never rewinds a live database.

## Versioning

Releases are fixed `vMAJOR.MINOR.PATCH` tags with an immutable GitHub Release.
While the version is 0.x, a minor release may change the wrapper contract, the
hook contract or the configuration variables; read the release notes before
moving a pin. Pin a consumer to a tag or a commit, never to `main`. How a
release is cut is in [CONTRIBUTING.md](CONTRIBUTING.md#releasing).

## Running the tests

See the [contributor test guide](test-harness/README.md) for prerequisites,
test commands, and fixture guidance.

CI type-checks Python with mypy in strict mode. In a Python 3.14 environment:

```sh
pip install -r requirements-dev.txt
mypy
```

Linting and formatting (shellcheck, shfmt, ruff, prettier) run in CI's
`baseline` job, from
[ci-baseline](https://github.com/curlewlabs-com/ci-baseline).

After a bump of CI's bats version, `python3 tools/refresh_test_tools.py`
rewrites the digest CI checks that archive against.

## License

Licensed under either of

- Apache License, Version 2.0 ([LICENSE-APACHE](LICENSE-APACHE))
- MIT license ([LICENSE-MIT](LICENSE-MIT))

at your option. Releases published under the MIT license alone may also be used
under either license.

### Contribution

By submitting a contribution for inclusion in this project, you agree that it
is dual licensed as above, without any additional terms or conditions.
