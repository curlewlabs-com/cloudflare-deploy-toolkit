"""The Workers runtime a version runs at, and the one the consumer expects.

A version's runtime - its compatibility date and flags - is fixed when it is
uploaded and recorded on the version itself, so a version that would run at the
wrong one can be refused before anything promotes it. The consumer states the
runtime it expects in DEPLOY_EXPECTED_RUNTIME, as the document
`{"compatibility_date": "YYYY-MM-DD", "compatibility_flags": [...]}`, read from
the same configuration its tests ran at; this module parses that, reads the
uploaded version's own, and says whether they differ.

Pure decisions over documents, AFFIRMATIVE ONLY like version_message.py: a
version document not shaped as Cloudflare's version schema describes is
Unreadable, never some default runtime, because a guessed runtime would make the
comparison vacuous. Unreadable is a separate outcome from a mismatch so the
operator is told which one stopped the deploy.
"""

from __future__ import annotations

import dataclasses
import datetime
import json
import re

# The example in Cloudflare's version schema writes the date as a midnight UTC
# timestamp, while `wrangler versions view --json` prints a bare `YYYY-MM-DD`;
# each names the same calendar day, and nothing else is accepted.
_DATE = re.compile(r"(\d{4}-\d{2}-\d{2})(?:T00:00:00Z)?")


class Unreadable(Exception):
    """A document that does not state a runtime this module can compare."""


@dataclasses.dataclass(frozen=True)
class Runtime:
    compatibility_date: str
    compatibility_flags: frozenset[str]

    def describe(self) -> str:
        flags = ", ".join(sorted(self.compatibility_flags))
        return (
            f"compatibility_date {self.compatibility_date}, "
            f"compatibility_flags [{flags}]"
        )


def _calendar_date(value: object, where: str) -> str:
    found = _DATE.fullmatch(value) if isinstance(value, str) else None
    if found is None:
        raise Unreadable(f"{where} compatibility_date is {value!r}, not a date")
    day = found.group(1)
    # The shape alone admits 2025-02-31, which names no day and so no runtime.
    try:
        datetime.date.fromisoformat(day)
    except ValueError as error:
        raise Unreadable(
            f"{where} compatibility_date {day} is not a calendar day"
        ) from error
    return day


def _flags(value: object, where: str) -> frozenset[str]:
    if not isinstance(value, list) or not all(isinstance(flag, str) for flag in value):
        raise Unreadable(
            f"{where} compatibility_flags is {value!r}, not a list of names"
        )
    return frozenset(value)


def expected(text: str) -> Runtime:
    """Parse DEPLOY_EXPECTED_RUNTIME. Raises Unreadable on anything else."""
    try:
        document: object = json.loads(text)
    except json.JSONDecodeError as error:
        raise Unreadable(f"DEPLOY_EXPECTED_RUNTIME is not JSON ({error})") from error
    if not isinstance(document, dict) or set(document) != {
        "compatibility_date",
        "compatibility_flags",
    }:
        raise Unreadable(
            "DEPLOY_EXPECTED_RUNTIME must be an object with exactly "
            "compatibility_date and compatibility_flags"
        )
    where = "DEPLOY_EXPECTED_RUNTIME's"
    return Runtime(
        _calendar_date(document["compatibility_date"], where),
        _flags(document["compatibility_flags"], where),
    )


def of_version(version: object) -> Runtime:
    """The runtime a version document records. Raises Unreadable.

    `version` is a document as `wrangler versions view --json` prints it, the
    `result` of Cloudflare's version endpoint
    (https://developers.cloudflare.com/api/resources/workers/subresources/scripts/subresources/versions/methods/get/),
    whose `resources.script_runtime` carries the runtime.
    """
    resources = version.get("resources") if isinstance(version, dict) else None
    script_runtime = (
        resources.get("script_runtime") if isinstance(resources, dict) else None
    )
    if not isinstance(script_runtime, dict):
        raise Unreadable("the version document has no resources.script_runtime")
    where = "the version's"
    date = _calendar_date(script_runtime.get("compatibility_date"), where)
    # The schema at the URL above marks compatibility_flags "optional" and says
    # "Responses omit the field when the list is empty", and a version with no
    # flags reads back without it. So absent is no flags; present and malformed
    # is still Unreadable.
    if "compatibility_flags" not in script_runtime:
        return Runtime(date, frozenset())
    return Runtime(date, _flags(script_runtime["compatibility_flags"], where))
