#!/usr/bin/env python3
"""CI-execution gate for service-mutating operations.

Operations that change a deployed service run in GitHub Actions, where their
workflows can supply scoped write credentials. Local recovery requires an
explicit override and a write-capable Cloudflare token.

Invoked as a command at each mutating chokepoint, like check_deploy_ref.py. Pass
a short operation label as the first argument for the refusal message.

The caller can select its own executable independently of WRANGLER, so that
variable is no basis for authorizing an operation. Tests that drive a guarded
script through a Wrangler double simulate the GitHub Actions environment.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping, Sequence

DEFAULT_OP = "a service-mutating operation"


def decide(argv: Sequence[str], env: Mapping[str, str]) -> tuple[int, list[str]]:
    """Return the exit code and the stderr lines for one invocation.

    Split from main() so the allow/deny matrix is a pure decision the tests can
    drive without a subprocess per row; main() owns the process boundary.
    """
    op = argv[0] if argv and argv[0] else DEFAULT_OP

    # Actions sets "true" exactly. A truthiness check would also accept "false".
    # Literal matches keep unset, empty, padded, and differently cased values
    # outside both authorization paths.
    if env.get("GITHUB_ACTIONS") == "true":
        return 0, []

    # Break-glass: an explicit, loud opt-in for a genuine CI-down emergency. It
    # only clears THIS gate - the operation still needs a write-capable token the
    # read-only default lacks, and a deploy still passes check_deploy_ref.py's
    # first-parent-main provenance gate.
    if env.get("ALLOW_LOCAL_DEPLOY") == "1":
        return 0, [
            f"[WARN] require-github-actions: ALLOW_LOCAL_DEPLOY=1 set - running {op} locally (break-glass).",
            "  Ensure a write-capable Cloudflare token is exported; the read-only default cannot mutate.",
        ]

    return 1, [
        f"[FAIL] require-github-actions: {op} changes the deployed service and must run in GitHub Actions, not from a local checkout.",
        "  Run it via its GitHub Actions workflow. If it has none, or CI is down and this is a genuine emergency, export a",
        "  write-capable token and set ALLOW_LOCAL_DEPLOY=1.",
    ]


def main(argv: Sequence[str]) -> int:
    exit_code, lines = decide(argv, os.environ)
    for line in lines:
        print(line, file=sys.stderr)
    return exit_code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
