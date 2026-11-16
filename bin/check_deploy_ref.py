#!/usr/bin/env python3
"""Deploy provenance gate.

Refuses to deploy any commit outside origin/main's first-parent history, so
production and staging run complete mainline trees with full source-control
trackability rather than incomplete merged PR tips.

Called at each deploy chokepoint: inside a service's deploy wrapper (so its
package.json deploy aliases, the deploy CI workflow, and direct operator runs all
pass through it) and in a simpler Worker's package.json deploy scripts (ahead of
`wrangler deploy`). It is
deliberately not wired into the deploy pipeline - the mock deploy tests exercise
that pipeline on an unmerged PR branch, where this gate would correctly refuse.

The modes:

  (no argument)  Vet HEAD - clean tree plus first-parent origin/main membership.
                 The tree being checked is the tree that deploys, so an
                 uncommitted edit is disqualifying.

  <sha>          Vet that commit alone. For a caller vetting a deploy target it
                 has not checked out: a deploy that selects a target older than
                 the workflow ref checks that target out and runs *its* copy of
                 this script, so the target would otherwise be vetted by whatever
                 rule it happened to ship with rather than the current one. Which
                 rule gets applied is therefore the caller's choice of checkout,
                 not this script's - a caller wanting the current rule must run
                 the current copy (from a checkout of main). No tree check - nothing deploys from the
                 caller's tree.

Unconditional by design - there is no override env. A genuine emergency lands the
fix on origin/main first (e.g. ALLOW_PUSH_TO_MAIN), then deploys from main; it
never deploys an off-mainline commit. Fails closed: any state it cannot prove
safe (no network to fetch main, shallow history it cannot deepen, a working-tree
read that errors, or - in HEAD mode - a dirty tree) is refused.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Sequence

# The explicit refspec (not a bare `main`) is required: a CI deploy checkout
# - actions/checkout pinned to a SHA - configures no remote-tracking fetch
# refspec, so `git fetch origin main` would write only FETCH_HEAD and leave
# origin/main unresolvable, making the merge-base below error out and fail-close
# an already-merged deploy. An explicit destination always writes
# refs/remotes/origin/main.
MAIN_REFSPEC = "+refs/heads/main:refs/remotes/origin/main"

# One deepen is enough for the depths actions/checkout produces here; a target
# older than this is a rollback far enough back that a full-history checkout is
# the honest fix rather than a larger constant.
DEEPEN_COMMITS = 200


class GateFailure(Exception):
    """A refusal carrying the operator-facing reason."""


def _git(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """Run git with output captured, never raising on a non-zero status.

    Every caller here decides what a failure means for itself - an unreachable
    merge-base is a deepen-and-retry, an unreadable working tree is a refusal -
    so the status is returned rather than thrown.
    """
    return subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _is_shallow() -> bool:
    git_dir = _git(["rev-parse", "--git-dir"])
    if git_dir.returncode != 0:
        return False
    return os.path.isfile(os.path.join(git_dir.stdout.strip(), "shallow"))


def _deepen() -> None:
    """Best-effort history deepen; the caller re-tests the real question after.

    A failure here is deliberately silent because it is not itself a verdict:
    the retry that follows is what decides, and it fails closed on its own.
    """
    _git(["fetch", "--quiet", f"--deepen={DEEPEN_COMMITS}", "origin", MAIN_REFSPEC])


def _is_ancestor_of_main(sha: str) -> bool:
    return _git(["merge-base", "--is-ancestor", sha, "origin/main"]).returncode == 0


def _is_first_parent_of_main(sha: str) -> bool:
    """Exact whole-line membership in main's first-parent chain.

    Exact rather than a prefix match, which is what refuses an abbreviated SHA:
    `merge-base --is-ancestor` resolves an abbreviation and would let one through
    the ancestry test above, and a caller that resolved only 7 characters has not
    named a unique deploy target.
    """
    listed = _git(["rev-list", "--first-parent", "origin/main"])
    if listed.returncode != 0:
        return False
    return sha in listed.stdout.splitlines()


def _validate_args(argv: Sequence[str]) -> None:
    """Reject a malformed invocation before any network work.

    Ahead of the fetch deliberately: a caller that passed no usable target gets
    told that, rather than a fetch failure from an offline runner standing in for
    the real complaint.
    """
    if len(argv) > 1:
        raise GateFailure("usage: check_deploy_ref.py [SHA]")

    # Length-tested rather than truthiness-tested so a present-but-empty argument
    # is distinguishable from no argument at all. It means the caller meant to
    # pass a resolved SHA and computed nothing; falling back to HEAD would vet the
    # caller's own tip - main's, for the deploy workflow - and pass trivially,
    # reporting success while checking nothing the caller asked about.
    if len(argv) == 1 and not argv[0]:
        raise GateFailure(
            "SHA argument is empty - the caller resolved no target to vet"
        )


def _resolve_target(argv: Sequence[str]) -> tuple[str, str]:
    """Return (sha, what) for the mode the arguments select."""
    if len(argv) == 1:
        return argv[0], "target"

    # A dirty tree means wrangler would bundle uncommitted code while
    # __DEPLOY_SHA__ is stamped from HEAD: what runs would not match what is in
    # git.
    # A non-zero status means git could not answer, which is not the same as an
    # empty answer: conflating them reads an unreadable tree as a clean one, in
    # a gate whose whole contract is to refuse what it cannot prove. No test
    # drives this arm and none can - git opens the index for `fetch` as well, so
    # every repo state that breaks the status read has already refused one step
    # earlier.
    status = _git(["status", "--porcelain"])
    if status.returncode != 0:
        raise GateFailure(
            "could not read the working tree state - cannot prove the deploy tree is clean"
        )
    if status.stdout.strip():
        raise GateFailure(
            "working tree is not clean - deploy only committed, merged code"
        )

    head = _git(["rev-parse", "HEAD"])
    sha = head.stdout.strip() if head.returncode == 0 else ""
    if not sha:
        raise GateFailure("could not resolve HEAD")
    return sha, "HEAD"


def run(argv: Sequence[str]) -> str:
    """Vet the selected target, returning the success line or raising GateFailure."""
    _validate_args(argv)

    # Refresh origin/main so the first-parent check sees current history, not a
    # stale ref.
    if _git(["fetch", "--quiet", "origin", MAIN_REFSPEC]).returncode != 0:
        raise GateFailure(
            "could not fetch origin/main - cannot verify the deploy commit is merged"
        )

    # Deliberately not resolved for the SHA mode: the caller resolves the target
    # out of band (the deploy workflow resolves it through the GitHub API), so a
    # target older than a shallow checkout's boundary is not in the object store
    # yet. The deepen-and-retry below is what brings it in - resolving here would
    # fail-close a legitimate mainline target.
    sha, what = _resolve_target(argv)

    # Reject commits outside even ordinary origin/main ancestry first. Deepen a
    # shallow clone once if the merge-base is not yet reachable; the exact
    # first-parent membership check below then rejects merged PR tips too.
    if not _is_ancestor_of_main(sha):
        if _is_shallow():
            _deepen()
        if not _is_ancestor_of_main(sha):
            raise GateFailure(
                f"{what} ({sha}) is not merged into origin/main - merge it first, then deploy from main"
            )

    # A merged PR tip is also an ordinary ancestor, but it is not a deployable
    # mainline state: its tree can omit first-parent changes incorporated by the
    # merge commit. Require exact membership on main's first-parent chain while
    # retaining older mainline rollback/roll-forward targets.
    if not _is_first_parent_of_main(sha):
        if _is_shallow():
            _deepen()
        if not _is_first_parent_of_main(sha):
            raise GateFailure(
                f"{what} ({sha}) is not a first-parent origin/main commit - deploy the merge/mainline SHA"
            )

    return f"[OK] deploy-ref: {what} ({sha}) is a first-parent origin/main commit"


def main(argv: Sequence[str]) -> int:
    try:
        print(run(argv))
    except GateFailure as failure:
        print(f"[FAIL] deploy-ref: {failure}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
