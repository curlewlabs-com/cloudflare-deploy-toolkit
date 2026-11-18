# Security policy

Report suspected vulnerabilities privately using
[GitHub private vulnerability reporting](https://github.com/curlewlabs-com/cloudflare-deploy-toolkit/security/advisories/new).
Include the affected release or commit, the wrangler version, reproduction
steps and the observed impact. Keep API tokens, account identifiers, database
IDs and personal data out of public issues, and out of any fixture or
diagnostics bundle you attach.

Curlew Labs maintains this repository; Jeffrey Wall
([jeffwall-curlewlabs](https://github.com/jeffwall-curlewlabs)) is the release
and security contact through GitHub. Reports are reviewed on a best-effort basis
without a guaranteed response time. Fixes target the latest release; older
release lines have no promised backport window. Report a vulnerability in
wrangler or in Cloudflare's API to Cloudflare as well.

## Trust boundary

The pipeline runs inside your deploy job, with that job's environment and the
Cloudflare credentials it gives wrangler. The hook commands your wrapper names
run as shell commands in that same environment; the pipeline does not sandbox
them, so a hook is as trusted as the wrapper that names it. The deployment
verifier sends `CLOUDFLARE_API_TOKEN` to Cloudflare's API and to no other host;
it reads the live build's SHA from the URL you give it, with only the header you
give it.

With `DEPLOY_DIAGNOSTICS_DIR` set, the pipeline copies the wrangler logs a run
produced into that directory, unredacted. Treat the bundle as you would those
logs before publishing it as a CI artifact. The test harness's sanitizer scrubs
the shapes it knows and the bindings it is told to when capturing fixtures; it
is not a general redactor, and `check-fixture-purity.sh` is the check that a
committed fixture carries nothing it should not.
