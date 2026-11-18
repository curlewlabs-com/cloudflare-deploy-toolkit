"""Bootstrap and serving-version decisions for a `wrangler deployments list
--json` document.

The decisions are pure: a document in, a verdict out, no Cloudflare connection.
They are here rather than inline in the pipeline because they are what the deploy's
rollback target and its TOCTOU re-checks are built on, and a decision that
can be driven against a real JSON shape with no connection is one that can be
tested against every shape that has to refuse.

AFFIRMATIVE ONLY, which is the same deny-on-unmodeled discipline the
consumer-side serving-set extractors follow. Neither answers "close enough". A
gradual split, a partial percentage, an unusable id, or a document that is not
the expected shape all refuse, because the caller's next move is to record a
rollback target or to assert that the serving set did not move underneath it -
and a wrong answer there is a rollback that restores the wrong code, or a
mid-deploy change nobody noticed. Refusing costs a deploy that has to be
re-dispatched; guessing costs the invariant.
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


def is_bootstrap_shape(raw: str) -> bool:
    """True when the document says this Worker has never served a version.

    The one emptiness a deploy may legitimately continue through: with no
    deployment, or no version under the newest one, there is no rollback target
    to record and nothing for the single-version invariant to describe. Kept
    apart from the extractor below so that "nothing has deployed yet" can never
    be reached by an extraction failure - otherwise a staging deploy proceeds
    with no rollback target on evidence nobody checked.
    """
    try:
        document = _parse(raw)
    except Refused:
        return False
    if not _is_json_array(document):
        return False
    if not document:
        return True
    newest = document[-1]
    if not _is_json_object(newest):
        return False
    versions = newest.get("versions")
    return _is_json_array(versions) and not versions


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
