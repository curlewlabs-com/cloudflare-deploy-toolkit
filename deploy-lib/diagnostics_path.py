"""Decides whether a path may be used as the deploy's diagnostics directory.

The deploy writes a failure bundle into `DEPLOY_DIAGNOSTICS_DIR` and its cleanup
runs `rm -rf` on the resolved path. That is the whole reason this decision is a
gate rather than a convenience: an operator who exports a value with a `.` or
`..` component gets a recursive delete somewhere they did not name, and the
canonicalization that resolves the parent would hide it - `/tmp/artifacts/..`
canonicalizes to `/tmp`, which is not a dedicated artifact directory.

So the rule is about the value as written, not where it resolves to. Refused:
any value made only of separators - the empty string, `/`, `//`, `///` - because
none of them names a directory a deploy may own, and any path with a `.` or `..`
component anywhere in it.

A value of only separators is worth naming outright, because it is the one that
reads as harmless. `//` has a real component in neither position, and the
preparation step that follows resolves it by taking its dirname and basename and
rejoining them - each of which is `/` - so the directory the cleanup deletes is
the filesystem root.

Nothing here touches the filesystem. This is a decision about a string, which is
what makes every case testable without a directory to delete.
"""

from __future__ import annotations

# Components that make a path unusable however cleanly its parent resolves.
TRAVERSAL_COMPONENTS = (".", "..")


def is_safe(candidate: str) -> bool:
    """True when this value may be used as a diagnostics directory."""
    # Separators only, at any length: nothing here is a component, so there is
    # no directory being named. Stripping rather than listing the forms,
    # because `/` and `//` are obvious forms while `///` is not.
    if not candidate.strip("/"):
        return False
    # Bounded before and after so the check is about whole components: a directory
    # named `.hidden` or `..config` contains the characters without being a
    # traversal, and refusing those would refuse a legitimate path.
    padded = f"/{candidate}/"
    return not any(f"/{component}/" in padded for component in TRAVERSAL_COMPONENTS)
