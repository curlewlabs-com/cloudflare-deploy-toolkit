#!/usr/bin/env python3
"""Provenance-gate tests: the gate must refuse anything that is not an exact
first-parent origin/main state, and any dirty tree in HEAD mode.

A regression that lets an off-mainline, PR-side-parent, or dirty-tree deploy
through is exactly the failure this guards - the whole trackability guarantee
rests on it.

HEAD and SHA modes are covered. HEAD mode (no argument) vets the tree that deploys. SHA
mode vets a target the caller has not checked out, which is what lets the deploy
workflow apply main's rule to an older target instead of leaving the target's own
copy of the gate to vet itself.

Each case builds a throwaway repo with a local bare "origin" and runs the gate as
a subprocess in it, so the real `git fetch` / `merge-base` / `rev-list` /
`status` logic runs end to end: no network, and no injected seam that could
weaken the gate under test.
"""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import override

GATE = Path(__file__).resolve().parents[2] / "bin" / "check_deploy_ref.py"


def git(cwd: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    )
    return done.stdout.strip()


class GateCase(unittest.TestCase):
    @override
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.origin = self.root / "origin.git"
        self.work = self.root / "work"

    def _configure(self, repo: Path) -> None:
        git(repo, "config", "user.email", "test@example.com")
        git(repo, "config", "user.name", "deploy-ref test")
        git(repo, "config", "commit.gpgsign", "false")
        # Detached auto-maintenance would outlive the commits and race the
        # fixture's own git commands and the tempdir cleanup.
        git(repo, "config", "maintenance.auto", "false")

    def setup_repo(self) -> Path:
        """A working repo whose `origin` is a local bare repo with a `main`
        branch, left with HEAD at the pushed main tip."""
        subprocess.run(
            ["git", "init", "--quiet", "--bare", str(self.origin)], check=True
        )
        # The push below runs receive-pack, which triggers the same detached
        # auto-maintenance a commit does - inside the tempdir being cleaned up.
        git(self.origin, "config", "maintenance.auto", "false")
        subprocess.run(["git", "init", "--quiet", str(self.work)], check=True)
        self._configure(self.work)
        git(self.work, "checkout", "--quiet", "-b", "main")
        (self.work / "file.txt").write_text("v1\n")
        git(self.work, "add", "file.txt")
        git(self.work, "commit", "--quiet", "-m", "initial")
        git(self.work, "remote", "add", "origin", str(self.origin))
        git(self.work, "push", "--quiet", "origin", "main")
        return self.work

    def setup_workflow_checkout(self) -> Path:
        """A checkout shaped like a CI deploy run - actions/checkout pinned to a
        SHA: shallow, detached, and crucially with NO
        `+refs/heads/*:refs/remotes/origin/*` fetch refspec, so
        refs/remotes/origin/main does not exist until the gate fetches it.

        setup_repo()'s `git remote add` configures that wildcard refspec and so
        hides this shape; the real deploy workflows do not have it.
        """
        self.setup_repo()
        main_tip = git(self.work, "rev-parse", "HEAD")
        checkout = self.root / "workflow-checkout"
        subprocess.run(["git", "init", "--quiet", str(checkout)], check=True)
        self._configure(checkout)
        # URL only - no fetch refspec, mirroring actions/checkout with a pinned SHA.
        git(checkout, "config", "remote.origin.url", str(self.origin))
        git(checkout, "fetch", "--quiet", "--depth=1", "origin", main_tip)
        git(checkout, "checkout", "--quiet", "--detach", main_tip)
        return checkout

    def run_gate(self, cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(GATE), *args], cwd=cwd, capture_output=True, text=True, check=False
        )

    def assertRefused(
        self, done: subprocess.CompletedProcess[str], needle: str
    ) -> None:
        self.assertEqual(done.returncode, 1, msg=done.stdout + done.stderr)
        self.assertIn(needle, done.stderr)

    def assertAccepted(self, done: subprocess.CompletedProcess[str]) -> None:
        self.assertEqual(done.returncode, 0, msg=done.stdout + done.stderr)
        self.assertIn("is a first-parent origin/main commit", done.stdout)

    def merge_a_feature(self, work: Path) -> str:
        """Land a merge commit on main and return the feature tip it absorbed.

        The tip is an ordinary ancestor of main whose tree omits the concurrent
        mainline change, which is the state HEAD and SHA modes have to refuse.
        """
        git(work, "checkout", "--quiet", "-b", "feature")
        (work / "feature.txt").write_text("feature\n")
        git(work, "add", "feature.txt")
        git(work, "commit", "--quiet", "-m", "feature work")
        feature_sha = git(work, "rev-parse", "HEAD")
        git(work, "checkout", "--quiet", "main")
        (work / "mainline.txt").write_text("mainline\n")
        git(work, "add", "mainline.txt")
        git(work, "commit", "--quiet", "-m", "concurrent mainline work")
        git(work, "merge", "--quiet", "--no-ff", "-m", "merge feature", "feature")
        git(work, "push", "--quiet", "origin", "main")
        return feature_sha


class HeadMode(GateCase):
    def test_merged_head_passes(self) -> None:
        self.assertAccepted(self.run_gate(self.setup_repo()))

    def test_unmerged_feature_commit_is_refused(self) -> None:
        work = self.setup_repo()
        git(work, "checkout", "--quiet", "-b", "feature")
        (work / "file.txt").write_text("v2\n")
        git(work, "commit", "--quiet", "-am", "unmerged work")
        self.assertRefused(self.run_gate(work), "not merged into origin/main")

    def test_older_mainline_commit_stays_deployable(self) -> None:
        """An older first-parent commit is a rollback/roll-forward target, so
        main advancing past HEAD is not by itself disqualifying."""
        work = self.setup_repo()
        merged_sha = git(work, "rev-parse", "HEAD")
        (work / "file.txt").write_text("v2\n")
        git(work, "commit", "--quiet", "-am", "later main work")
        git(work, "push", "--quiet", "origin", "main")
        git(work, "checkout", "--quiet", merged_sha)
        self.assertAccepted(self.run_gate(work))

    def test_merged_pr_second_parent_is_refused(self) -> None:
        """A merged PR tip is an ordinary ancestor of main, but its tree can omit
        concurrent first-parent changes included by the merge commit."""
        work = self.setup_repo()
        feature_sha = self.merge_a_feature(work)
        git(work, "checkout", "--quiet", "--detach", feature_sha)
        self.assertRefused(self.run_gate(work), "not a first-parent origin/main commit")

    def test_dirty_tracked_file_is_refused(self) -> None:
        """The deployed bundle would not match the committed SHA."""
        work = self.setup_repo()
        with (work / "file.txt").open("a") as handle:
            handle.write("dirty\n")
        self.assertRefused(self.run_gate(work), "working tree is not clean")

    def test_gitignored_build_output_does_not_trip_the_clean_check(self) -> None:
        """A static site may generate a sitemap and a version file at deploy
        time, so ignored output must not read as a dirty tree."""
        work = self.setup_repo()
        (work / ".gitignore").write_text("build/\n")
        git(work, "add", ".gitignore")
        git(work, "commit", "--quiet", "-m", "ignore build output")
        git(work, "push", "--quiet", "origin", "main")
        (work / "build").mkdir()
        (work / "build" / "artifact.txt").write_text("generated\n")
        self.assertAccepted(self.run_gate(work))

    def test_ci_checkout_without_a_remote_tracking_ref_passes(self) -> None:
        """The gate must populate refs/remotes/origin/main itself: a bare
        `git fetch origin main` leaves it unresolvable in this shape and would
        falsely reject a real, already-merged deploy."""
        self.assertAccepted(self.run_gate(self.setup_workflow_checkout()))


class ShaMode(GateCase):
    def test_older_mainline_target_passes(self) -> None:
        """The deploy workflow resolves a production target and vets it from
        main's checkout, so HEAD sits on a later commit than the target."""
        work = self.setup_repo()
        older_sha = git(work, "rev-parse", "HEAD")
        (work / "file.txt").write_text("v2\n")
        git(work, "commit", "--quiet", "-am", "later main work")
        git(work, "push", "--quiet", "origin", "main")
        done = self.run_gate(work, older_sha)
        self.assertAccepted(done)
        self.assertIn(f"target ({older_sha})", done.stdout)

    def test_merged_pr_second_parent_is_refused(self) -> None:
        """The case the workflow's SHA input cannot catch alone: it accepts any
        commit the API resolves, and a tip predating the first-parent rule
        carries a bare-ancestry gate that would accept itself."""
        work = self.setup_repo()
        feature_sha = self.merge_a_feature(work)
        self.assertRefused(
            self.run_gate(work, feature_sha), "not a first-parent origin/main commit"
        )

    def test_unmerged_target_is_refused(self) -> None:
        work = self.setup_repo()
        git(work, "checkout", "--quiet", "-b", "feature")
        (work / "file.txt").write_text("v2\n")
        git(work, "commit", "--quiet", "-am", "unmerged work")
        unmerged_sha = git(work, "rev-parse", "HEAD")
        git(work, "checkout", "--quiet", "main")
        self.assertRefused(
            self.run_gate(work, unmerged_sha), "not merged into origin/main"
        )

    def test_empty_argument_is_refused_rather_than_falling_back_to_head(self) -> None:
        """A gate that passes vacuously is worse than one that is absent, because
        it reads as proof."""
        work = self.setup_repo()
        self.assertRefused(self.run_gate(work, ""), "SHA argument is empty")

    def test_dirty_tree_is_ignored(self) -> None:
        """Nothing deploys from the caller's checkout, so a stray generated file
        on the runner must not fail a promotion for an unrelated reason."""
        work = self.setup_repo()
        target_sha = git(work, "rev-parse", "HEAD")
        with (work / "file.txt").open("a") as handle:
            handle.write("dirty\n")
        self.assertAccepted(self.run_gate(work, target_sha))

    def test_abbreviated_sha_is_refused(self) -> None:
        """`merge-base --is-ancestor` resolves an abbreviation, so only the exact
        whole-line membership test refuses a caller that resolved 7 characters
        and named no unique deploy target."""
        work = self.setup_repo()
        short_sha = git(work, "rev-parse", "--short=7", "HEAD")
        self.assertRefused(
            self.run_gate(work, short_sha), "not a first-parent origin/main commit"
        )

    def test_extra_arguments_are_refused(self) -> None:
        work = self.setup_repo()
        sha = git(work, "rev-parse", "HEAD")
        self.assertRefused(self.run_gate(work, sha, sha), "usage:")


class UnreadableWorkingTree(GateCase):
    """The status-read refusal has no case of its own, and the reason is worth
    recording where the next reader looks for one: every repo state that makes
    `git status` fail also makes the `git fetch` ahead of it fail, because git
    opens the index for the status and the fetch. A corrupt or unreadable index refuses at the fetch,
    which is the same closed direction one step earlier."""

    def test_an_unreadable_index_refuses_before_the_tree_is_read(self) -> None:
        work = self.setup_repo()
        (work / ".git" / "index").write_bytes(b"not an index")
        self.assertRefused(self.run_gate(work), "could not fetch origin/main")


if __name__ == "__main__":
    unittest.main()
