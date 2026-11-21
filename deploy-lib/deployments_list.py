"""Extract the single serving version from Wrangler's deployment history.

A gradual split has no unique rollback target. Unreadable or unexpected shapes
must stop the deploy before the version is used for recovery or re-verification.
"""

from __future__ import annotations

import json
from typing import TypeGuard


class Refused(Exception):
    """The document does not carry an answer this module will give."""


def _is_json_object(value: object) -> TypeGuard[dict[str, object]]:
    """True for a decoded JSON object. Its keys are strings, which `isinstance`
    alone cannot tell a type checker."""
    return isinstance(value, dict)


def _is_json_array(value: object) -> TypeGuard[list[object]]:
    return isinstance(value, list)


def _parse(raw: str) -> object:
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise Refused(f"deployments list is not valid JSON: {exc}") from exc


def single_serving_version_id(raw: str) -> str:
    """The version_id of the newest deployment's single serving version.

    Only when that deployment carries exactly one version and it serves at
    percentage 100. Every other shape raises Refused with the detail, which the
    caller prints: the operator has to resolve a split deliberately, and a
    message naming which shape was found is what tells them how.
    """
    document = _parse(raw)
    if not _is_json_array(document) or not document:
        raise Refused("deployments list is not a non-empty array")

    newest = document[-1]
    versions = newest.get("versions") if _is_json_object(newest) else None
    if not _is_json_array(versions):
        raise Refused("the newest deployment carries no versions array")
    if len(versions) != 1:
        raise Refused(
            f"expected exactly one version in the newest deployment, found {len(versions)}"
        )

    entry = versions[0]
    # An entry that is not an object reads as one with no fields, which the
    # percentage check below refuses.
    fields: dict[str, object] = entry if _is_json_object(entry) else {}
    percentage = fields.get("percentage")
    # One comparison covers the type too: nothing `json.loads` can produce is
    # equal to 100 without being a number, so a string, a bool, a null, or a
    # missing key all fail here rather than needing an arm of their own.
    if percentage != 100:
        raise Refused(
            "the single version is not serving at percentage 100 "
            f"(got {json.dumps(percentage)})"
        )

    version_id = fields.get("version_id")
    if not isinstance(version_id, str) or not version_id:
        raise Refused("serving version entry has no usable version_id")
    return version_id
