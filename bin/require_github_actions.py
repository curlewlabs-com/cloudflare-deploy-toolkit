#!/usr/bin/env python3
"""CI-execution gate for service-mutating operations.

Worker/website deploys, D1 schema applies, DB unlocks, and FCM secret rotations
change the deployed service, so they run in GitHub Actions - where the scoped
write tokens live - not from a local checkout. This is the in-repo half of the
privilege split; the capability half is that dev machines default to a read-only
Cloudflare token, so a local run also needs a deliberately-exported write token. Running in Actions and exporting that
token are deliberate acts, never an oopsie.

Invoked as a command at each mutating chokepoint, like check_deploy_ref.py. Pass
a short operation label as the first argument for the refusal message.

Authorizes ONLY on GITHUB_ACTIONS=true or the ALLOW_LOCAL_DEPLOY break-glass -
deliberately no WRANGLER-name escape hatch. This gate is decoupled from the CLI a
guarded script actually runs (a secret-rotation script may hardcode its own
wrangler and take no override), so keying a skip off WRANGLER=...mock-wrangler.sh would clear
the gate while the real CLI still mutated - a genuine bypass. Local tests that
drive a guarded script through a wrangler double simulate CI (GITHUB_ACTIONS=
true) instead, and a deploy wrapper that refuses any WRANGLER value outright
needs no skip carved for it.

Class-Audit note for the allow-capable input class: the `GITHUB_ACTIONS` and `ALLOW_LOCAL_DEPLOY` authorizing reads are
whole-string equality against a single literal, not a pattern, so no token class
widens the allow set - an unset, empty, differently-cased, or whitespace-padded
value is a refusal. `GITHUB_ACTIONS` is compared to "true" rather than tested for
truthiness because Actions sets that exact string and a truthiness test would
authorize on "false".
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

    # GitHub Actions sets GITHUB_ACTIONS=true; that is the sanctioned path.
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
        '  write-capable token and set ALLOW_LOCAL_DEPLOY=1.',
    ]


def main(argv: Sequence[str]) -> int:
    exit_code, lines = decide(argv, os.environ)
    for line in lines:
        print(line, file=sys.stderr)
    return exit_code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
