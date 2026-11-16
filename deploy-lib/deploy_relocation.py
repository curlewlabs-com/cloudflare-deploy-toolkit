"""A relocation's source and target: what state they are in, and who may open which.

A relocation moves a Worker off the database it is serving from - the SOURCE -
onto the one this checkout deploys against - the TARGET. It fences the source,
has the consumer copy it, promotes a version bound to the target, and opens the
target. The Worker serves throughout, since no maintenance version is promoted:
bound to the source until the promote, and to the target after it. What it
refuses while a lock is set is the consumer's own lock contract - writes at the
least, and for a consumer that checks the lock on every request, those requests
too - from the fence to the moment the target opens.

THE ONLY DURABLE RECORD IS THEIR DEPLOY LOCKS, and that is deliberate. A lock
survives a dead process, and no copy can overwrite one: the copy never writes
the source's, and it leaves the target's set. Read together, before anything is
mutated, they say how far an earlier run got:

  source open,   target absent - not started. Fence, copy, promote, open.
  source fenced, target absent - fenced, and no copy landed. Go forward, if
                                 the operator says so (below).
  source fenced, target locked - a copy landed and was never opened. Go
                                 forward on the same condition; the copy hook
                                 proves the copy still exact.
  source open,   target locked - an attempt copied, then backed out. The copy
                                 is already stale and this pipeline drops
                                 nothing, so it refuses until the target has
                                 been emptied.
  target open                  - the relocation is complete, or this database
                                 was never a relocation's target. Refuse: what
                                 is being asked for is an ordinary deploy.

"Absent" is a target with no lock table, which is how a database that was never
filled reads. deploy_lock reports that as unlocked, because for a deploy it is;
here it is the difference between a target nothing has touched and one that is
already serving.

A FENCED SOURCE NEEDS A PERSON TO SAY WHY. The lock is one bit and cannot say
who set it: a deploy that died inside its maintenance window leaves the same
bit an earlier relocation run does, over a database that may be part-way through
a migration. So a run continues from a fenced source only when the operator has
acknowledged a resume (DEPLOY_RELOCATION_RESUME), and the acknowledgement is
refused when the source turns out to be open, because then the operator's
picture of the state is wrong and the run stops rather than guess which part.

WHO MAY REOPEN THE SOURCE. Only the run that fenced it, and only until it
reaches the promote. A run that found the source already fenced cannot know
whether the earlier run's promote applied, and a run past its own promote
cannot know that either - the call can apply server-side and still report
failure. Reopening the source in either case could hand writes to a database
the serving version no longer reads. So those runs never reopen the source.
Until the body's last step opens the target, that leaves source and target
locked, which refuses every write and loses none, and the next dispatch goes
forward. Once that step has opened the target the relocation is complete,
whatever fails after it: the target takes the writes, the source stays fenced
for good, and what is left is an ordinary deploy's problem.

NOTHING HERE ROLLS THE WORKER BACK. Until the target opens, whichever version
serves does so from a locked database, so there is no stale write for a
rollback to prevent, and a rollback would only turn a promote that may have
applied into one more thing the next run has to reason about. After it opens, a
rollback would put back a version bound to the fenced source.
"""

from __future__ import annotations

import dataclasses
import time

import deploy_config
import deploy_database
import deploy_lock
import deploy_reporting
import deploy_state
import lock_state
from deploy_reporting import emit, emit_error
from deploy_state import DeployAbort


@dataclasses.dataclass(frozen=True)
class Start:
    """Where a relocation may start from. `resumed` means the source was already fenced."""

    resumed: bool


class Refused(Exception):
    """The source's and target's locks describe a state a relocation must not run from."""


def start_from(
    source: str, target: str, *, target_absent: bool, resume_acknowledged: bool
) -> Start:
    """Decide from one read of each lock. The module docstring has the table.

    `source` and `target` are deploy_lock verdicts. Anything that is not
    positively locked or unlocked refuses, as it does for every other body.
    """
    known = (lock_state.LOCKED, lock_state.UNLOCKED)
    if source not in known or target not in known:
        raise Refused(
            "a deploy lock could not be read, and a relocation is decided by "
            f"the pair (source={source}, target={target})"
        )
    if target == lock_state.UNLOCKED and not target_absent:
        raise Refused(
            "the target's deploy lock reads clear, so it is already open: the "
            "relocation is complete, or this database was never its target. "
            "Run an ordinary deploy"
        )
    if target == lock_state.LOCKED and source == lock_state.UNLOCKED:
        raise Refused(
            "the target holds a locked copy and the source is open, so an "
            "earlier attempt copied and then backed out. That copy is stale and "
            "this pipeline drops nothing; empty the target, then re-dispatch"
        )
    fenced = source == lock_state.LOCKED
    if fenced and not resume_acknowledged:
        raise Refused(
            "the source is already locked, and a lock cannot say who set it. If "
            "an earlier relocation run fenced it, re-dispatch acknowledging the "
            "resume; if a deploy left it locked, resolve that deploy first"
        )
    if resume_acknowledged and not fenced:
        raise Refused(
            "a resume was acknowledged and the source is not fenced, so the "
            "state is not the one the dispatch assumed. Read the source and target locks before "
            "dispatching again"
        )
    return Start(resumed=fenced)


class Relocation:
    """The steps a relocation takes on its source and its target."""

    def __init__(
        self,
        config: deploy_config.DeployConfig,
        state: deploy_state.PipelineState,
        *,
        reporter: deploy_reporting.Reporter,
        source: deploy_database.Database,
        source_state: deploy_state.PipelineState,
        source_lock: deploy_lock.LockReader,
        target: deploy_database.Database,
        target_lock: deploy_lock.LockReader,
    ) -> None:
        self._config = config
        self._state = state
        self._reporter = reporter
        self._source = source
        # The source database's own lock fields. deploy_database writes the
        # lock state and the owed unlock onto the state it was given, and the
        # run's state already carries those for the target.
        self._source_state = source_state
        self._source_lock = source_lock
        self._target = target
        self._target_lock = target_lock

    def read_start_state(self) -> None:
        """Read each lock and refuse any state a relocation must not run from."""
        config, state = self._config, self._state
        self._reporter.step(
            f"Read the relocation's source and target locks ({config.env})"
        )
        if config.dry_run:
            emit("[dry-run] source open, target absent: a relocation not yet started")
            return

        source = self._source_lock.capture(
            "read the relocation source's deploy lock before mutating anything",
            deploy_lock.PRECHECK,
        )
        target = self._target_lock.capture(
            "read the relocation target's deploy lock before mutating anything",
            deploy_lock.PRECHECK,
        )
        state.lock_state = target
        absent = self._target_lock.last_table_absent
        emit(
            f"source {config.relocation_source_db}: {source}; "
            f"target {config.db_name}: {'absent' if absent else target}"
        )
        try:
            start = start_from(
                source,
                target,
                target_absent=absent,
                resume_acknowledged=config.relocation_resume,
            )
        except Refused as refusal:
            emit_error(f"[FAIL] Refusing to relocate: {refusal}.")
            raise DeployAbort(1) from refusal
        state.relocation_resumed = start.resumed
        if start.resumed:
            emit(
                "-> The source is already fenced, so this run resumes an earlier "
                "one. It will go forward, and it will not reopen the source."
            )

    def fence_source(self) -> None:
        """Lock the source, unless an earlier run already did.

        Starts the pause budget either way. On a resumed run the source has
        been fenced since the earlier run closed it, so the budget there bounds
        what this run adds rather than the pause as a whole - and a breach
        still reopens nothing, because a resumed run never may.
        """
        self._state.relocation_pause_deadline = (
            time.monotonic() + self._config.relocation_max_pause_seconds
        )
        if self._state.relocation_resumed:
            emit("")
            emit("> Source already fenced by an earlier run - nothing to lock.")
            return
        if not self._config.dry_run:
            # Read again, because the uploads since the first read took long
            # enough for something else to take this lock. Locking over another
            # holder would read back as locked, and this run would then believe
            # the fence was its own to reopen.
            again = self._source_lock.capture(
                "confirm the relocation source is still open before fencing it",
                deploy_lock.PRECHECK,
            )
            if again != lock_state.UNLOCKED:
                emit_error(
                    "[FAIL] The relocation source's lock changed since this run "
                    f"read it (now {again}). Something else holds it; this run "
                    "fenced nothing and reopens nothing."
                )
                raise DeployAbort(1)
        self._source.lock()

    def verify_still_closed(self) -> None:
        """Read each lock back before the promote, and refuse unless each is set.

        The copy and migrate hooks ran in between, and a hook's exit status is
        not the state of a lock. A target whose lock a hook cleared would start
        taking writes the moment the promote applied - before the serving
        version is verified, and with recovery still reasoning as though it
        were shut. A source whose lock was cleared has taken writes the copy
        does not hold. Each is refused here, while this run may still back out.
        """
        config = self._config
        self._reporter.step(
            f"Verify source and target are still locked before promoting ({config.env})"
        )
        if config.dry_run:
            emit("[dry-run] verify the source and the target still read locked")
            return
        source = self._source_lock.capture(
            "confirm the relocation source is still fenced before the promotion",
            deploy_lock.LOCK,
        )
        target = self._target_lock.capture(
            "confirm the relocation target is still locked before the promotion",
            deploy_lock.LOCK,
        )
        self._state.lock_state = target
        if source == lock_state.LOCKED and target == lock_state.LOCKED:
            emit("Source still fenced; target still locked.")
            return
        emit_error(
            "[FAIL] Refusing to promote: the source reads "
            f"{source} and the target reads {target}, and each must read locked."
        )
        if target == lock_state.UNLOCKED:
            emit_error(
                f"  {config.db_name} holds a copy and its lock is CLEAR. Nothing "
                "serves from it yet, so it takes no writes; it must be emptied "
                "before another relocation will start."
            )
        raise DeployAbort(1)

    def refuse_if_the_pause_is_spent(self) -> None:
        """Abort, while this run may still back out, if the budget has run out.

        The fenced hooks are stopped at the deadline, so this catches what is
        left: steps that are not hooks, and a hook that finished just inside it.
        Past the promote nothing is reopened, so this is the last point at
        which running long can still be answered by giving the source back.
        """
        deadline = self._state.relocation_pause_deadline
        if deadline is None or time.monotonic() <= deadline:
            return
        emit_error(
            "[FAIL] The relocation's pause budget of "
            f"{self._config.relocation_max_pause_seconds}s is spent and the "
            "promote has not been reached. Not promoting."
        )
        raise DeployAbort(1)

    def mark_promote_reached(self) -> None:
        """From the next call on, the source is never reopened by this run."""
        self._state.relocation_promote_reached = True

    def open_target(self) -> None:
        """Clear the target's lock, read it back, and refuse on anything else.

        The last mutation of a relocation and the one that completes it: from
        here the target is the only database taking writes.
        """
        if not self._target.unlock():
            raise DeployAbort(1)

    @property
    def pause_deadline(self) -> float | None:
        """When the fenced hooks must have finished by, or None before the fence."""
        return self._state.relocation_pause_deadline

    # --- recovery ----------------------------------------------------------

    @property
    def target_is_open(self) -> bool:
        """Whether this run opened the target, read back. Then it is complete."""
        return self._state.unlock_result == "success"

    @property
    def source_lock_state(self) -> str:
        """The source's lock as this run last read it, for the diagnostics bundle.

        The run's own state carries the target's, as it does on every body.
        """
        return self._source_lock.last.state

    @property
    def source_reopen_is_owed(self) -> bool:
        """Whether this run fenced the source and may still give it back.

        The owed unlock is written only by this run's own fence, so a run that
        found the source already fenced owes nothing here. deploy_database keeps
        it owed when the lock could not be read back: a fence nobody could
        verify is not a fence nobody took.
        """
        return (
            self._source_state.unlock_required
            and not self._state.relocation_promote_reached
        )

    def reopen_source(self) -> bool:
        """Unlock the source after a failure before the promote. False if unproven."""
        reopened = self._source.unlock()
        self._state.source_unlock_result = self._source_state.unlock_result
        return reopened
