#!/usr/bin/env python3
"""Allow/deny matrix for the CI-execution gate.

The risk includes a hole that lets a service-mutating op run
from a laptop, or an over-tight gate that breaks the local deploy/unlock test
harnesses (which simulate CI to drive the guarded scripts through wrangler
doubles).

Regression locked in below: a wrangler-double NAME in the environment must NOT
self-authorize - the gate is decoupled from the CLI a guarded script actually
runs, so treating the name as a skip was a real off-CI bypass.

The environment is passed in explicitly rather than patched onto os.environ,
which is what makes the deny rows meaningful: CI runs this suite with
GITHUB_ACTIONS=true, and an inherited environment would mask every one of them.
"""

from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path

BIN_DIR = Path(__file__).resolve().parents[2] / "bin"
sys.path.insert(0, str(BIN_DIR))

from require_github_actions import DEFAULT_OP, decide

GATE = BIN_DIR / "require_github_actions.py"


class AllowRows(unittest.TestCase):
    def test_github_actions_true_allows_silently(self) -> None:
        self.assertEqual(decide(["op"], {"GITHUB_ACTIONS": "true"}), (0, []))

    def test_break_glass_allows_with_a_loud_warning(self) -> None:
        code, lines = decide(["op"], {"ALLOW_LOCAL_DEPLOY": "1"})
        self.assertEqual(code, 0)
        self.assertIn("ALLOW_LOCAL_DEPLOY=1 set - running op locally", lines[0])
        self.assertIn("write-capable Cloudflare token", lines[1])


class DenyRows(unittest.TestCase):
    def test_bare_local_run_is_refused(self) -> None:
        code, lines = decide(["op"], {})
        self.assertEqual(code, 1)
        self.assertIn("must run in GitHub Actions", lines[0])

    def test_wrangler_double_name_alone_does_not_self_authorize(self) -> None:
        """Treating the name as a skip is a real off-CI bypass: a guarded script
        can hardcode its own wrangler, so the double's name says nothing about
        what would actually run. Only CI or break-glass authorizes."""
        for double in ("/x/mock-wrangler.sh", "/x/fake-wrangler.sh"):
            with self.subTest(double=double):
                code, _ = decide(["op"], {"WRANGLER": double})
                self.assertEqual(code, 1)

    def test_real_wrangler_without_ci_is_refused(self) -> None:
        code, _ = decide(["op"], {"WRANGLER": "npx wrangler"})
        self.assertEqual(code, 1)

    def test_near_miss_values_do_not_authorize(self) -> None:
        """Whole-string equality keeps near misses around `true` and `1` out."""
        for env in (
            {"GITHUB_ACTIONS": "false"},
            {"GITHUB_ACTIONS": "TRUE"},
            {"GITHUB_ACTIONS": " true"},
            {"GITHUB_ACTIONS": ""},
            {"ALLOW_LOCAL_DEPLOY": "0"},
            {"ALLOW_LOCAL_DEPLOY": "true"},
            {"ALLOW_LOCAL_DEPLOY": ""},
        ):
            with self.subTest(env=env):
                code, _ = decide(["op"], env)
                self.assertEqual(code, 1)


class OperationLabel(unittest.TestCase):
    def test_label_rides_into_the_refusal(self) -> None:
        _, lines = decide(["unlock-db production"], {})
        self.assertIn("unlock-db production changes the deployed service", lines[0])

    def test_missing_or_empty_label_falls_back(self) -> None:
        for argv in ([], [""]):
            with self.subTest(argv=argv):
                _, lines = decide(argv, {})
                self.assertIn(DEFAULT_OP, lines[0])


class ProcessBoundary(unittest.TestCase):
    """One row per exit path through the real executable, so the shebang, the
    exit code, and the stderr routing are covered rather than assumed."""

    def _run(self, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        # PATH supplies the selected Python for the real shebang without
        # inheriting any environment value that can authorize the operation.
        process_env = {"PATH": os.environ["PATH"], **env}
        return subprocess.run(
            [str(GATE), "op"],
            env=process_env,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_allow_path_exits_zero_and_says_nothing_on_stdout(self) -> None:
        done = self._run({"GITHUB_ACTIONS": "true"})
        self.assertEqual(done.returncode, 0)
        self.assertEqual(done.stdout, "")

    def test_deny_path_exits_one_and_writes_to_stderr(self) -> None:
        done = self._run({})
        self.assertEqual(done.returncode, 1)
        self.assertEqual(done.stdout, "")
        self.assertIn("must run in GitHub Actions", done.stderr)


if __name__ == "__main__":
    unittest.main()
