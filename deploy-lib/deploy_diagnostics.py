"""The failure bundle a deploy leaves behind, and the directory it owns.

When `DEPLOY_DIAGNOSTICS_DIR` is set the pipeline writes one `key=value` file
recording how the run ended and what recovery it performed, plus copies of only
the wrangler logs this run produced. It is meant to be a CI failure artifact, so
it has to be readable after the job that wrote it is gone.

THE DIRECTORY IS OWNED, NOT SHARED, and that is a safety property rather than a
convention: preparation clears the directory with a recursive delete, so it
refuses to touch a path that exists without this library's ownership marker in
it, and it refuses a path value that could resolve somewhere its writer did not
name. Which values are refusable is diagnostics_path.py, which owns why a `.` or
`..` component is unusable however cleanly the parent resolves.

The path is checked before creation and before deletion because those checks
answer different questions. The creation check runs on the operator's value, so
a refusable value costs no directories. The deletion check sits on the delete itself, on the
absolute path it is about to remove, because that string is the one with
consequences and today's resolution step is not the only one that could ever
produce it.
"""

from __future__ import annotations

import os
import pathlib
import re
import shutil
import tempfile
from collections.abc import Sequence

import deploy_reporting
import diagnostics_path

BUNDLE_NAME = "deploy-diagnostics.txt"
MANIFEST_NAME = "wrangler-log-manifest.txt"
LOGS_DIR_NAME = "wrangler-logs"
OWNER_MARKER = ".deploy-lib-owned"
START_MARKER = ".started-at"

# Wrangler's Linux per-user, macOS per-user, and project-local log directories,
# searched in this order. A run can produce logs in
# more than one.
LOG_DIR_TEMPLATES = (
    "{home}/.config/.wrangler/logs",
    "{home}/Library/Preferences/.wrangler/logs",
    "{cwd}/.wrangler/logs",
)
LOG_GLOB = "wrangler-*.log"

# ASCII whitespace only, written out rather than taken from a locale-aware
# class: the bundle is read on whichever host collected it, and a value must not
# compact differently depending on which that was. Same reading the transient
# roster takes, for the same reason.
_ASCII_SPACE_RUN = re.compile(r"[ \t\n\r\f\v]+")

# Every value in here can carry wrangler's own text, and wrangler_runner decodes
# the API's streams with surrogateescape so the retry classifier reads the bytes
# Cloudflare actually sent. A strict encode would raise on one - a ValueError,
# which the OSError guards below do not catch - and the bundle is written on the
# way out of a deploy that has already finished its recovery, so the raise would
# turn a completed run into a traceback. The artifact is read by a person
# reconstructing an incident, so rendering the byte beats losing the file. Same
# policy, same reason, as the pipeline's stdout (deploy_reporting).
RENDER_POLICY = "backslashreplace"


class Refused(Exception):
    """The configured directory is not one this library may own."""


def compact(value: str) -> str:
    """One line, for a `key=value` bundle field.

    A newline in a value would silently add a field to a file that is parsed by
    splitting on lines, so every value that can carry wrangler's text goes
    through here first.
    """
    return _ASCII_SPACE_RUN.sub(" ", value).strip(" ")


def _unusable(value: str) -> str:
    """The refusal an operator sees, naming the value that was refused."""
    return (
        "DEPLOY_DIAGNOSTICS_DIR must point to a dedicated artifact directory, "
        f"got '{value}'"
    )


class Diagnostics:
    """The diagnostics directory for one run, and what gets written into it."""

    def __init__(
        self, configured_dir: str | None, *, reporter: deploy_reporting.Reporter
    ) -> None:
        self._configured = configured_dir
        self._reporter = reporter
        self._dir: pathlib.Path | None = None
        self._start_marker: pathlib.Path | None = None
        self.log_count = 0

    @property
    def enabled(self) -> bool:
        return self._dir is not None

    def prepare(self) -> None:
        """Claim the directory, or raise Refused with what to tell the operator."""
        if self._configured is None:
            return

        if not diagnostics_path.is_safe(self._configured):
            raise Refused(_unusable(self._configured))

        # Trailing separators come off BEFORE the split. POSIX `dirname` and
        # `basename` strip them first, and Python's do not: `os.path.dirname`
        # reads `/tmp/artifacts/` as the directory `/tmp/artifacts` with an empty
        # name, which would make the preparation step create the target and then
        # refuse it as an existing directory it does not own. `is_safe` has
        # already refused every value that is only separators, so what is left
        # after the strip is a real name.
        configured = self._configured.rstrip("/")
        parent = os.path.dirname(configured)
        name = os.path.basename(configured)
        os.makedirs(parent or ".", exist_ok=True)
        resolved = os.path.join(os.path.abspath(parent or "."), name)

        target = pathlib.Path(resolved)
        owner_marker = target / OWNER_MARKER
        if target.exists() and not owner_marker.is_file():
            raise Refused(
                "DEPLOY_DIAGNOSTICS_DIR already exists without a deploy-lib "
                f"ownership marker: {resolved}\n"
                "  Point DEPLOY_DIAGNOSTICS_DIR at a dedicated artifact directory "
                "so cleanup cannot wipe an unrelated path."
            )

        remove_target(target)
        target.mkdir(parents=True)
        owner_marker.write_text("", encoding="utf-8")
        self._dir = target
        self._start_marker = target / START_MARKER
        self._start_marker.write_text("", encoding="utf-8")

    def collect_wrangler_logs(self) -> bool:
        """Copy in the wrangler logs this run created. False on an I/O failure.

        Only logs newer than the start marker, because the point is the run that
        just failed: a per-user log directory accumulates every wrangler call the
        host has ever made, and an artifact carrying all of them is one nobody
        reads.
        """
        if self._dir is None or self._start_marker is None:
            return True

        logs_dir = self._dir / LOGS_DIR_NAME
        manifest = self._dir / MANIFEST_NAME
        try:
            started_at = self._start_marker.stat().st_mtime_ns
            logs_dir.mkdir(parents=True, exist_ok=True)
            candidates = self._logs_newer_than(started_at)
        except OSError as exc:
            self._reporter.ci_error(f"wrangler log scan failed: {exc}")
            return False

        count = 0
        manifest_lines: list[str] = []
        for source in candidates:
            destination = logs_dir / f"{count + 1:02d}-{source.name}"
            try:
                shutil.copyfile(source, destination)
            except OSError:
                self._reporter.ci_error(f"wrangler log copy failed: {source}")
                continue
            manifest_lines.append(f"{source}\t{destination}")
            count += 1

        try:
            manifest.write_text(
                "".join(f"{line}\n" for line in manifest_lines),
                encoding="utf-8",
                errors=RENDER_POLICY,
            )
        except OSError as exc:
            self._reporter.ci_error(f"wrangler log manifest write failed: {exc}")
            return False

        self.log_count = count
        return True

    def write_bundle(self, rows: Sequence[tuple[str, str]]) -> bool:
        """Write the `key=value` bundle. False on an I/O failure.

        Written to a temporary file in the same directory and moved into place,
        so a reader that arrives while the deploy is still failing never sees a
        half-written bundle.
        """
        if self._dir is None:
            return True
        body = "".join(f"{key}={value}\n" for key, value in rows)
        try:
            handle, temporary = tempfile.mkstemp(
                dir=str(self._dir), prefix="deploy-diagnostics."
            )
            with os.fdopen(handle, "w", encoding="utf-8", errors=RENDER_POLICY) as sink:
                sink.write(body)
            os.replace(temporary, self._dir / BUNDLE_NAME)
        except OSError as exc:
            self._reporter.ci_error(f"diagnostics bundle write failed: {exc}")
            return False
        return True

    def _logs_newer_than(self, started_at_ns: int) -> list[pathlib.Path]:
        found: set[pathlib.Path] = set()
        home = os.environ.get("HOME")
        for template in LOG_DIR_TEMPLATES:
            # A run without HOME set skips the Linux and macOS per-user directories rather
            # than searching a placeholder; the project-local one still applies.
            if "{home}" in template and not home:
                continue
            directory = pathlib.Path(template.format(home=home or "", cwd=os.getcwd()))
            if not directory.is_dir():
                continue
            for candidate in directory.rglob(LOG_GLOB):
                if candidate.is_file() and candidate.stat().st_mtime_ns > started_at_ns:
                    found.add(candidate)
        return sorted(found)


def remove_target(target: pathlib.Path) -> None:
    """`rm -rf` on a path this library has established it owns.

    The safety decision is re-asked here rather than trusted from the caller,
    because this function is the recursive delete: whatever resolution produced
    the path, the string being removed is checked immediately before it is.
    """
    if not diagnostics_path.is_safe(str(target)):
        raise Refused(_unusable(str(target)))
    if target.is_symlink() or target.is_file():
        target.unlink()
    elif target.is_dir():
        shutil.rmtree(target)
