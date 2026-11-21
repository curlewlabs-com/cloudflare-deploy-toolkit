"""How far a deploy got, and how one ends.

The recovery path's whole decision is a function of how far the deploy got, and
this record is what "how far" means: whether a maintenance version is serving,
whether the database is locked and whether an unlock is owed, whether a bookmark
exists to restore from, which version to roll back to, and whether the final
promote was in flight. The steps write it, the recovery path reads it, and
nothing outside this process can touch it: a consumer's hook runs as a
subprocess, so it can fail a deploy but cannot alter the recovery decision.

`DeployAbort` lives here too, because the exit status is the last field of the
same record: a step that refuses raises it, the recovery path still runs, and
the status it carries is what the run exits with.
"""

from __future__ import annotations

import dataclasses


class DeployAbort(Exception):
    """A step refused to continue. Recovery still runs; this carries the status."""

    def __init__(self, exit_code: int = 1) -> None:
        super().__init__(f"deploy aborted with exit code {exit_code}")
        self.exit_code = exit_code


@dataclasses.dataclass
class PipelineState:
    """One run's mutable state, shared by the steps and the recovery path."""

    # --- what the Worker is serving ---
    prev_version_id: str = ""
    new_maint_id: str = ""
    new_final_id: str = ""
    maint_live: bool = False
    # True only while the final promote is in flight. The windowless body never
    # sets maint_live, so this is what tells the recovery path to roll the
    # Worker back when the promote may have applied server-side while the CLI
    # still failed. Cleared on success, so a later failure never rolls back a
    # promote that landed.
    final_promote_attempted: bool = False
    # The same, for the maintenance promote, and set for the same reason: the
    # call can apply server-side and still report failure, while maint_live is
    # only set after it returns. Without this the recovery path would exit clean
    # on a promote that actually landed, leaving the Worker serving maintenance
    # mode - a 503 for every request - while the run reports "old version still
    # serving". Cloudflare's gradual-deployment model being atomic does not
    # answer this: atomicity decides whether the promotion happened, not whether
    # the client learned that it did.
    maint_promote_attempted: bool = False
    # The single serving version the windowless capture recorded: the reference
    # every TOCTOU re-check matches against. Identical to prev_version_id at
    # capture time, kept under its own name so the re-checks read as what they
    # are rather than as a rollback concern.
    serving_version_id: str = ""

    # --- what the database is doing ---
    bookmark: str = ""
    lock_state: str = "unlocked"
    # Kept apart from lock_state on purpose: a lock attempt that could not be
    # verified leaves the state unknown and the unlock still owed, and
    # collapsing the two would skip the unlock in exactly that case.
    unlock_required: bool = False

    # --- a relocation's source and target (deploy_relocation.py) ---
    # The source was found already fenced, so an earlier run fenced it. This run
    # cannot know whether that run's promote applied, and so never reopens it.
    relocation_resumed: bool = False
    # Set before the relocation's promote and never cleared, unlike
    # final_promote_attempted, which a successful promote clears. From here the
    # target-bound version may be what serves, and reopening the source would
    # hand writes to a database nothing reads any more - so a failure AFTER a
    # promote that succeeded must leave the source fenced too.
    relocation_promote_reached: bool = False
    source_unlock_result: str = "not_run"
    # The monotonic instant by which a relocation must have reached its promote,
    # set when the fence closes. None until then, and on every other body.
    relocation_pause_deadline: float | None = None

    # --- what recovery did, which is what the diagnostics bundle reports ---
    d1_restore_result: str = "not_needed"
    rollback_result: str = "not_needed"
    unlock_result: str = "not_run"
    failed_step: str = "none"

    def uploaded_version(self, version_id: str) -> bool:
        """Only an id returned by this run can still be propagating."""
        return bool(version_id) and version_id in (self.new_maint_id, self.new_final_id)

    @property
    def unlock_is_safe(self) -> bool:
        """Whether clearing the lock re-admits writes against known-good code.

        The unlock is an obligation, not a recovery layer, and discharging it is
        normally right: a lock left set refuses the next deploy. There is one
        state where discharging it is worse than leaving it. The final promote
        can apply server-side and still report failure, and when it does the
        recovery path tries to put the previous version back. If that rollback
        does not succeed, the newly promoted version - real code serving real
        traffic, not the maintenance version - may still be live, while the D1
        restore has already taken the schema back to before the migration.
        Unlocking there admits writes from new code against an old schema.

        So the lock stays set exactly there, because it is the last thing
        stopping those writes, and the next deploy refusing to start is the
        intended consequence rather than a cost.

        The other such state is a D1 restore that failed. The migrate ran and
        then something failed, so the schema may be part-way between the
        bookmark and the migration, and the restore that should have taken it
        back did not. Whatever the rollback put back, unlocking admits writes
        into that half-migrated database, and those writes are what nothing
        afterwards can untangle; a locked database is recovered through the
        unlock workflow once someone has looked.

        Everything else unlocks, including a failed rollback with only the
        maintenance version live: that version returns 503 unconditionally, so
        no write can land whatever is serving, and holding the lock would block
        the next deploy for nothing.

        This cannot be False on a green run: the flag below is cleared when the
        promote succeeds, and the recovery path that sets rollback_result and
        d1_restore_result only runs on a failure.
        """
        if self.d1_restore_result == "failed":
            return False
        if not self.final_promote_attempted:
            return True
        return self.rollback_result == "success"


def shell_bool(value: bool) -> str:
    """`true`/`false`, the written form the diagnostics bundle has always used."""
    return "true" if value else "false"
