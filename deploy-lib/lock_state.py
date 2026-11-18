"""Classifies one read of the `_deploy_lock` row into the deploy's view of the
database: locked, unlocked, or unreadable.

The read is a `wrangler d1 execute --json` call, and what comes back is not one
shape but several: a result set, an empty result set, an API error envelope, or
nothing parseable at all. This module owns which of those means what.

THE THIRD ANSWER IS THE IMPORTANT ONE. "Unreadable" is not a synonym for
"unlocked" - the caller retries an unreadable state on its own backoff and
ultimately refuses the deploy, because a lock it could not read is a lock it
cannot say is clear. Every shape that is not positively one or the other lands
there, so a value nobody modeled stalls a deploy instead of walking through a
lock somebody else is holding.

The one exception is the table not existing at all, which is a positive answer
rather than a gap: a database that has never carried `_deploy_lock` has nobody
holding it, and a first-ever deploy has to be able to proceed.
"""

from __future__ import annotations

import json
import re
from typing import TypeGuard

LOCKED = "locked"
UNLOCKED = "unlocked"

# The API's wording when the lock table has never been created. Anchored at the
# start because the message carries more after it - the statement, or
# `: SQLITE_ERROR [code: 7500]` - and closed at the table's name, so that
# another table whose name merely begins the same way (`_deploy_lock_archive`)
# is not read as this one.
NO_SUCH_TABLE = re.compile(r"no such table: _deploy_lock(?![A-Za-z0-9_])")


class Unreadable(Exception):
    """The read produced no verdict. Carries what to show an operator."""


def _is_json_object(value: object) -> TypeGuard[dict[str, object]]:
    """True for a decoded JSON object. Its keys are strings, which `isinstance`
    alone cannot tell a type checker."""
    return isinstance(value, dict)


def _is_json_array(value: object) -> TypeGuard[list[object]]:
    return isinstance(value, list)


def _load(raw: str) -> object:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def names_missing_lock_table(stdout: str) -> bool:
    """True when a failed read failed only because the table is not there yet.

    Public because a relocation has to tell this apart from a lock that reads
    clear, which `classify` deliberately does not: a target with no lock table
    has never been filled, and one whose lock reads clear is already serving.
    """
    document = _load(stdout)
    if not _is_json_object(document):
        return False
    error = document.get("error")
    if not _is_json_object(error):
        return False
    return any(NO_SUCH_TABLE.match(text) for text in _error_texts(error))


def _error_texts(error: dict[str, object]) -> list[str]:
    """Every place wrangler has been seen to put the API's own message.

    Older wrangler put it in the error's `text`. Since its version 4, wrangler
    puts its own sentence there ("A request to the Cloudflare API (...)
    failed.") and the API's message in `notes`, one `{"text": ...}` per note -
    observed against real D1, not inferred. Only these positions are read: a message that merely
    appears somewhere in the document is not the API saying it.
    """
    texts = [error.get("text")]
    notes = error.get("notes")
    if _is_json_array(notes):
        texts.extend(note.get("text") for note in notes if _is_json_object(note))
    return [text for text in texts if isinstance(text, str)]


def _rows(document: object) -> list[object] | None:
    """The result rows, or None when the document is not the shape D1 returns."""
    if not _is_json_array(document) or not document:
        return None
    first = document[0]
    if not _is_json_object(first):
        return None
    results = first.get("results")
    return results if _is_json_array(results) else None


def classify(exit_code: int, stdout: str, stderr: str) -> str:
    """LOCKED or UNLOCKED, or raise Unreadable with what an operator should see.

    stdout is preferred over stderr in the failure payload because wrangler puts
    the API's own error document there; stderr is the fallback for a call that
    died before writing one.
    """
    if exit_code != 0:
        if names_missing_lock_table(stdout):
            return UNLOCKED
        raise Unreadable(stdout or stderr)

    rows = _rows(_load(stdout))
    if rows is None:
        raise Unreadable(stdout or stderr)
    if not rows:
        # The table exists and holds no row: nobody has ever taken the lock.
        return UNLOCKED

    first_row = rows[0]
    locked = first_row.get("locked") if _is_json_object(first_row) else None
    # Rendered to text and compared whole, so the integer D1 returns and the
    # string a different driver might return are the same answer, and nothing
    # else is. No type arm is needed to reject `true`, a null, a missing column,
    # or a container: none of them renders as exactly "1" or "0", so they all
    # reach the refusal below with everything else this module does not model.
    # Whole-string, not a substring: "01" and "10" are not a lock state.
    if str(locked) == "1":
        return LOCKED
    if str(locked) == "0":
        return UNLOCKED
    raise Unreadable(stdout or stderr)
