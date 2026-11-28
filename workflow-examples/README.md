# workflow-examples

Copy-paste-and-adapt GitHub Actions YAML for the deploy + verify pipeline of a
stateful Cloudflare Worker using cloudflare-deploy-toolkit.

## Files

- **`deploy.yml`**: Full deploy workflow: pre-deploy checks (manual only),
  change detection (anchored to GitHub Deployments API), dependency install,
  deployment-record start (`gh api` calls to the Deployments REST API), run
  `deploy-safe.sh`, failure-only artifact upload, post-deploy verification via
  the Cloudflare API, deployment-record finish (a status POST with
  `if: always()`)

## How to adapt `deploy.yml` for a new Worker

The example uses `<WORKER>`, `<PROJECT>`, `<I18N_PATH>`, and `<BUN_VERSION>` as
placeholder markers. Search for `CONSUMER:` comments - they flag every line that
needs a project-specific decision.

Specifically:

1. **Worker name** - replace `<WORKER>` in workflow / job / concurrency labels
   and in the verify step's `SCRIPT_NAME` values. A typical convention is
   `<short-name>-api` (production) and `<short-name>-api-staging` (staging).

2. **Project path prefix** - replace `<PROJECT>` with the path to your service
   directory (e.g. `services/api`, or for a monorepo `my-product/service`). Used
   in the `working-directory:` and the `path:` fields of the artifact upload.

3. **Change-detection (anchor and paths)** - the change-detection step calls
   `gh api /repos/{owner}/{repo}/deployments?environment=<WORKER>-staging` to
   find the SHA of the most recent successful deployment record (the
   deployment-record steps further down the workflow create the records this
   query reads). It then `git diff`s against that SHA over a list of path globs.
   This anchors deploys to "what's actually live on Cloudflare" - runs that
   skipped via the change-detector, never reached deploy (CI failure, cancel),
   or failed mid-deploy don't move the anchor. The Deployments REST API doesn't
   filter by status, so the example walks the most recent 20 deployments
   newest-first and takes the first whose latest status is `success`. To adapt:
   - Replace the path globs with everything that should trigger a redeploy (your
     service directory, any shared i18n / config / lockfile that affects the
     bundle, and the toolkit pin or submodule path). Include the toolkit path
     when staging should exercise an upgrade to the deploy procedure.
   - **Keep the step's `Anchor -> HEAD` echo.** The other end of the diff is
     `github.event.workflow_run.head_sha`, the commit CI validated - not the
     head SHA the Actions UI and the runs API report for the run, which on a
     `workflow_run` event is the default branch's tip when the event fired. The
     two differ whenever the branch moves during CI, so without that line an
     operator reproducing a skip from the run page diffs a range the job never
     evaluated and reads a correct no-op as a dropped deploy.
   - **Namespace the environment name by service.** The change-detector and the
     deployment-record steps register under `<WORKER>-staging` /
     `<WORKER>-production`. Sharing a bare `staging` namespace with another
     deploy workflow in the same repo lets one service's success advance the
     other service's anchor to a SHA where the other service was never deployed,
     causing skips that drop real changes. Replace `<WORKER>` with your service
     identifier in the `ENV_NAME=` line and in the start step's `RECORD_ENV`.
   - The example declares `permissions: { contents: read, deployments: write }`
     at the workflow level. Don't drop `deployments: write` - the anchor query
     and the deployment-record steps need it.
   - The runner needs `gh` on PATH for the `gh api` calls in the
     change-detector. GitHub-hosted Ubuntu images (e.g. `ubuntu-24.04`) ship it
     preinstalled; on a self-hosted runner provision it up front. Do not pin
     against the floating `ubuntu-latest` tag - pick a versioned label from
     <https://docs.github.com/en/actions/reference/runners/github-hosted-runners>.

4. **Cloudflare account ID** - set `CLOUDFLARE_ACCOUNT_ID` explicitly. Do not
   auto-discover it with `/accounts`: the restricted account tokens commonly
   used for deploys cannot rely on that endpoint, and the verify helper reads
   the explicit account ID from the workflow environment.

5. **Self-hosted vs hosted runner** - `runs-on: self-hosted` is the default in
   the example because it assumes a runner-local cache directory
   (`<RUNNER_CACHE_DIR>`) that outlives each job. If you're moving off
   self-hosted, change EACH `runs-on:` line (the `checks` job and the `deploy`
   job - they must match) to a pinned GitHub-hosted Ubuntu label (e.g.
   `ubuntu-24.04` - pick a current pinned label from
   <https://docs.github.com/en/actions/reference/runners/github-hosted-runners>;
   never `ubuntu-latest`), and drop the `BUN_INSTALL_CACHE_DIR` / `BUN_INSTALL`
   env vars at the top of the workflow - and with them the `local-mutex` wrapper
   around `bun install`, which exists only because those vars point every runner
   instance on a host at one cache. Keep or drop the vars and the lock together:
   the vars without the lock leave concurrent lanes writing one store that bun
   promises nothing about, and the lock without the vars serializes installs
   that share nothing.

6. **Package manager** - replace bun with npm/pnpm/yarn if needed. The
   `oven-sh/setup-bun@v2` action and `bunx` invocations all assume bun.

7. **Pre-deploy checks** - `<PROJECT>/scripts/check.sh` is your check pipeline.
   Run your project's lint, type checks, and tests before a manual deploy.

8. **Deploy script** - `<PROJECT>/scripts/deploy-safe.sh` is your safe-deploy
   wrapper, ~30 lines of boilerplate plus a few lines per hook. It exports
   `DEPLOY_WRANGLER_ENV_*` plus `PREFLIGHT_CMD` / `MIGRATE_CMD` /
   `POSTFLIGHT_CMD`, and runs
   `safe_deploy_pipeline.py "$@"`. See
   [`../deploy-lib/README.md`](../deploy-lib/README.md) for the full API and an
   inlined wrapper example.

9. **Toolkit location and verification** - make the toolkit available in the
   deploy job before running the wrapper. For the default submodule location,
   `vendor/cloudflare-deploy-toolkit`, keep submodule checkout enabled. For a
   [release archive](../README.md#release-archive), read the tag and digest
   from the checked-out revision alongside its wrapper. A `workflow_run`
   job can check out an earlier revision than the one providing the workflow
   YAML, so keeping the pin with the wrapper couples their versions. Run the
   archive setup in a separate step, which writes `DEPLOY_TOOLKIT_DIR` to
   `GITHUB_ENV` after successful verification and extraction. Both the wrapper
   and `Verify deployment` use that path.

   The example verifies the deployment timestamp. If your Worker echoes its
   build SHA, also export `VERIFY_DEPLOYMENT_EXPECTED_SHA` (the commit you
   deployed) and `VERIFY_DEPLOYMENT_SHA_URL` to verify that build is live.
   Optional settings include `VERIFY_DEPLOYMENT_SHA_HEADER` (a request
   header needed to reach the URL, e.g. a WAF gate header),
   `VERIFY_DEPLOYMENT_SHA_JQ` (the jq filter for the SHA field, default `.sha`),
   and `VERIFY_DEPLOYMENT_SHA_ATTEMPTS` /
   `VERIFY_DEPLOYMENT_SHA_INTERVAL_SECONDS` (the propagation poll budget,
   default 6 x 5s).

## Why a workflow example matters

Most of the value of this toolkit is in the **shape of the deploy workflow** -
knowing where the failure-only artifact upload step goes, knowing the
post-deploy verification needs to use the Cloudflare API (because Bot Fight Mode
blocks `curl`), knowing the change-detection logic needs to fall back to
deploying when in doubt. None of that is hard once you've seen it written down.
All of it is easy to get subtly wrong on the first try.

The example is a working, reviewed-in-production reference. Treat the inline
comments as the documentation - they explain _why_ each step looks the way it
does.

## What's NOT in this example

- **CI workflow** (`ci.yml`) for the same Worker. Build/lint/test before the
  deploy workflow can run. Not included here because CI is project- specific in
  a way deploy isn't (the deploy pipeline is mostly the same shape across
  Workers; CI varies more by toolchain).
- **Secret rotation workflow.** Out of scope for the deploy story.
