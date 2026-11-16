"""Finish a test-tool bump in CI: rewrite the shellcheck and bats digests.

A bump moves `SHELLCHECK_VERSION` or `BATS_VERSION` in
.github/workflows/ci.yml. The sha256 of each archive CI installs follows from
that version and cannot be typed from it, so this downloads each archive for
the version the file now names and writes its digest beside it.

    python3 tools/refresh_test_tools.py

It fails, changing nothing, when the workflow does not name exactly one of each
version or digest line it rewrites, or when an archive cannot be downloaded.
"""

from __future__ import annotations

import hashlib
import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/ci.yml"

_SHELLCHECK = "https://github.com/koalaman/shellcheck/releases/download/{version}/"

# Each digest line CI checks, with the release archive it is the digest of. A
# `{version}` is the tag the workflow names, with its `v`.
ARCHIVES = {
    "SHELLCHECK_SHA256_LINUX_X86_64": (
        "SHELLCHECK_VERSION",
        _SHELLCHECK + "shellcheck-{version}.linux.x86_64.tar.xz",
    ),
    "SHELLCHECK_SHA256_DARWIN_AARCH64": (
        "SHELLCHECK_VERSION",
        _SHELLCHECK + "shellcheck-{version}.darwin.aarch64.tar.xz",
    ),
    "BATS_SHA256": (
        "BATS_VERSION",
        "https://github.com/bats-core/bats-core/archive/refs/tags/{version}.tar.gz",
    ),
}


class RefreshError(Exception):
    """The workflow is not in a state this can finish a bump from."""


def _line(name: str) -> re.Pattern[str]:
    return re.compile(rf"^(?P<lead>\s*{name}: )(?P<value>\S+)$", re.MULTILINE)


def _one(text: str, name: str) -> re.Match[str]:
    matches = list(_line(name).finditer(text))
    if len(matches) != 1:
        raise RefreshError(
            f"{WORKFLOW.name} names {name} {len(matches)} times, not once"
        )
    return matches[0]


def _download(url: str) -> bytes:
    try:
        with urllib.request.urlopen(url, timeout=60) as response:
            body: bytes = response.read()
            return body
    except OSError as e:
        raise RefreshError(f"could not download {url}: {e!r}") from e


def refresh(text: str) -> str:
    """`text` with each digest line set to its archive's sha256."""
    digests: dict[str, str] = {}
    for digest_name, (version_name, url) in ARCHIVES.items():
        _one(text, digest_name)
        version = _one(text, version_name)["value"]
        archive = _download(url.format(version=version))
        digests[digest_name] = hashlib.sha256(archive).hexdigest()
    for digest_name, sha256 in digests.items():
        match = _one(text, digest_name)
        text = text[: match.start("value")] + sha256 + text[match.end("value") :]
    return text


def main() -> int:
    try:
        text = WORKFLOW.read_text()
        updated = refresh(text)
    except RefreshError as e:
        print(f"refresh_test_tools: {e}", file=sys.stderr)
        return 1
    if updated != text:
        WORKFLOW.write_text(updated)
        print(
            f"refresh_test_tools: rewrote the digests in {WORKFLOW.relative_to(ROOT)}"
        )
    else:
        print("refresh_test_tools: every digest already matches its archive")
    return 0


if __name__ == "__main__":
    sys.exit(main())
