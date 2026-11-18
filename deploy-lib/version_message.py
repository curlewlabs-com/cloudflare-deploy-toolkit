"""The message a pipeline upload writes on a Worker version, and the commit a
version's message names.

An upload writes `deploy-pipeline <label> (<sha>)`, then ` commit=<full sha>`
when the consumer gives the full commit, then a space and the consumer's
annotation when it has one, as the version's `workers/message` annotation. A
version's annotations are immutable, so its message still names the commit it
was uploaded from when a later deployment - a rollback from the dashboard, say
- makes it serve again. That is what lets a reader say which commit is serving
from nothing but the serving version.

Only the full commit identifies one. The parenthesised `<sha>` is the short
form the consumer displays, and distinct commits in one history share short
prefixes, so a reader that matched on it could take a rolled-back version for
the build it was asked about. A message without `commit=` - every version
uploaded before it was written, or by a consumer that does not pass it - names
no commit.

Only a final upload names a commit meant to serve. A maintenance upload's
version serves only inside a window, and a version uploaded outside this
pipeline carries whatever message its uploader typed; neither answers "which
commit is serving".

A pure decision over one document, AFFIRMATIVE ONLY like deployments_list.py:
anything not shaped as a final upload's message with its full commit names
none.
"""

from __future__ import annotations

import re
from typing import TypeGuard

MAINTENANCE = "maintenance"
FINAL = "final"

FINAL_MESSAGE = re.compile(
    r"deploy-pipeline final \([^()\s]*\) commit=([0-9a-f]{40})(?: .*)?", re.DOTALL
)


def upload_message(label: str, sha: str, commit: str, annotation: str) -> str:
    message = f"deploy-pipeline {label} ({sha})"
    if commit:
        message = f"{message} commit={commit}"
    return f"{message} {annotation}" if annotation else message


def _is_json_object(value: object) -> TypeGuard[dict[str, object]]:
    """True for a decoded JSON object. Its keys are strings, which `isinstance`
    alone cannot tell a type checker."""
    return isinstance(value, dict)


def final_commit(version: object) -> str | None:
    """The full commit a version's final-upload message names, or None.

    `version` is a version document as `wrangler versions view --json` prints
    it, which is the `result` of Cloudflare's version endpoint.
    """
    annotations = version.get("annotations") if _is_json_object(version) else None
    message = (
        annotations.get("workers/message") if _is_json_object(annotations) else None
    )
    found = FINAL_MESSAGE.fullmatch(message) if isinstance(message, str) else None
    return found.group(1) if found else None
