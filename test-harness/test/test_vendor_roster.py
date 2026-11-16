#!/usr/bin/env python3
"""Guards the toolkit's published install contract.

The toolkit's README documents a single-file-curl install: a list of paths an
out-of-tree consumer fetches one at a time. That list is the interface, and
nothing in CI follows it, so adding a
file to `deploy-lib/` that the toolkit then refuses to start without leaves the
published install fetching an incomplete tree. The break is silent here and
total there: the consumer's next deploy fails at startup with a file they were
never told to fetch.

The invariant is simple enough to derive rather than enumerate: everything in
`deploy-lib/` except its README is loaded at deploy time - the shell entry point
a wrapper's hook scripts source, the pipeline the wrapper starts, and the Python
modules those two resolve and refuse to run without - so every one of them has
to appear in the fetch list. Deriving it is the point. An enumerated roster would
need editing in lockstep with the very change it exists to catch, which is the
edit that gets forgotten.

This is a cross-boundary contract assertion, not a change detector: its inputs
come from independent surfaces - the tree and the published instructions - and
it fails only when a consumer following those instructions would get a broken
install.
"""

from __future__ import annotations

import pathlib
import re
import unittest
from typing import override

TOOLKIT = pathlib.Path(__file__).resolve().parents[2]
README = TOOLKIT / "README.md"
DEPLOY_LIB = TOOLKIT / "deploy-lib"

# The install block is a `for f in <paths>; do curl ...` loop. Read the loop's
# word list rather than the whole block, so prose mentioning a path elsewhere in
# the README cannot stand in for fetching it.
_FETCH_LOOP = re.compile(r"^for f in (.*?); do$", re.MULTILINE | re.DOTALL)


def fetched_paths(readme_text: str) -> set[str]:
    """The paths the documented curl install actually fetches."""
    match = _FETCH_LOOP.search(readme_text)
    if match is None:
        return set()
    # The loop wraps across lines with a trailing backslash.
    words = match.group(1).replace("\\\n", " ").split()
    return {word for word in words if "/" in word}


def deploy_time_paths() -> set[str]:
    """Everything under deploy-lib/ that a deploy loads, README excluded."""
    return {
        f"deploy-lib/{path.name}"
        for path in sorted(DEPLOY_LIB.iterdir())
        if path.is_file() and path.name != "README.md"
    }


class VendorFetchRoster(unittest.TestCase):
    @override
    def setUp(self) -> None:
        # A curl-installed copy has no README, and a partial vendor is not this
        # repo's contract to check - the same "absent is a skip, not a failure"
        # the Python runners already use for an unprovisioned dependency.
        if not README.is_file() or not DEPLOY_LIB.is_dir():
            self.skipTest("no toolkit README or deploy-lib beside this suite")

    def test_the_install_fetches_every_file_a_deploy_loads(self) -> None:
        fetched = fetched_paths(README.read_text())
        self.assertTrue(fetched, f"no `for f in ...; do` fetch loop found in {README}")
        missing = sorted(deploy_time_paths() - fetched)
        self.assertEqual(
            missing,
            [],
            "The single-file-curl install block in "
            "README.md does not fetch "
            f"{missing}. A consumer following it gets a tree the toolkit "
            "refuses to start on. Add the path(s) to that block's file list.",
        )

    def test_the_install_fetches_nothing_that_left_deploy_lib(self) -> None:
        stale = sorted(
            path
            for path in fetched_paths(README.read_text())
            if path.startswith("deploy-lib/") and not (TOOLKIT / path).is_file()
        )
        self.assertEqual(
            stale,
            [],
            "The single-file-curl install block in "
            "README.md fetches "
            f"{stale}, which no longer exists. A consumer following it gets a "
            "404 mid-install. Remove the path(s) from that block's file list.",
        )


if __name__ == "__main__":
    unittest.main()
