# Contributing

Thanks for your interest in improving `cloudflare-deploy-toolkit`. The bar for a
change is whether a deploy that fails at any step still gets the recovery that
step calls for, and never mistakes a wrangler exit status for what happened on
Cloudflare. Bug fixes, a transient wrangler failure shape seen in a real run, a
failure path the test harness does not yet exercise, and clearer docs are all
welcome.

## Before a large change

Open an issue describing the problem first. The recovery layers and the unlock
obligation in `deploy-lib/deploy_recovery.py`, the `_deploy_lock` read-backs in
`deploy-lib/deploy_lock.py`, and the relocation's lock states in
`deploy-lib/deploy_relocation.py` carry the safety guarantees, and
[`deploy-lib/README.md`](deploy-lib/README.md) says what each body does when a
step fails. A short discussion up front saves rework.

## Releasing

A patch or minor release needs no approval: whoever cuts it follows the steps
below in order, on a `main` commit whose required checks have passed. A major
version, a 1.0, or a minor before 1.0 that changes an interface consumers use
needs the maintainer's explicit approval before its tag is pushed.

A release is a fixed `vMAJOR.MINOR.PATCH` tag and the GitHub Release published
on it, carrying `cloudflare-deploy-toolkit-<tag>.tar.gz` and `SHA256SUMS`.
Consumers pin the archive's URL and digest (README, "Release archive"), so a
release reaches only someone who moves their pin to it. There is no floating
tag.

Release when `main` changes a file a consumer runs or copies since the last tag.
A fix is a patch release. While the version is 0.x, a change to the wrapper
contract, the hook contract or the configuration variables is a minor release
(README, "Versioning").

1. Pick the `main` commit to release. Its CI and CodeQL runs must have passed.
2. Tag it and push the tag:

   ```sh
   git tag v0.2.1 <commit>
   git push origin v0.2.1
   ```

   The Release workflow refuses a tag whose commit is not already on `main`,
   runs CI again on the tagged commit, and then builds the archive with
   `git archive` of the tagged tree, writes `SHA256SUMS`, and creates a draft
   release holding both.

3. Check the draft's archive against one built locally from the same tag with
   the workflow's own command. The two digests must be equal, and equal to the
   draft's `SHA256SUMS`:

   ```sh
   TAG=v0.2.1
   git archive --format=tar.gz --prefix="cloudflare-deploy-toolkit-$TAG/" "$TAG" \
     | shasum -a 256
   gh release view "$TAG" --json assets --jq '.assets[] | .name + " " + .digest'
   ```

4. Replace the draft's placeholder notes and publish it. The notes say what a
   consumer has to read before moving a pin: what was removed, what was fixed,
   and the limits that remain, each item naming its issue where it has one.

   ```sh
   gh release edit "$TAG" --notes-file notes.md --draft=false
   ```

Publishing is one-way. The published release and its assets are immutable, and
the fixed-version-tag ruleset keeps the tag from being moved or deleted, which
is what lets a consumer pin the archive by digest. A mistake in a published
release is fixed by the next release, never by editing this one.

## License

By submitting a contribution for inclusion in this project, you agree that it
is licensed under the MIT and Apache-2.0 licenses, as the README's License
section describes, without any additional terms or conditions.
