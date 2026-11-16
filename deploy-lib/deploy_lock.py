"""Reading the `_deploy_lock` row, and remembering what each read said.

The reads happen in a deploy and they answer different questions: before
anything is mutated (is somebody else holding the lock?), after the lock
mutation (did it apply?), and after the unlock (did that apply?). Each one is
recorded separately, because the diagnostics bundle an operator reads after a
failure has to say which of the three could not be answered - "the lock is
unreadable" means something different at each point.

WHY THIS READ IS NOT WRAPPED IN THE TRANSIENT RETRY. The loop here retries on
ANY unreadable state, which is strictly broader than the transient roster:
whatever the reason a lock could not be read, another attempt is worth making
before refusing the deploy. Putting the narrower classifier underneath would
reduce that to the shapes somebody has enumerated.

What one read means - locked, unlocked, or unreadable - is lock_state.py, which
owns every shape and why the third answer is not a synonym for the second.
"""

from __future__ import annotations

import dataclasses
import random
import time
from collections.abc import Sequence

import deploy_reporting
import lock_state
import retry_sleep
import wrangler_runner

# The value the pipeline and the diagnostics bundle carry when no read has
# produced an answer. Deliberately neither lock state: every caller refuses on
# it, which is the direction a lock nobody can read has to fail in.
UNKNOWN = "unknown"

# Phases, which are the pre-mutation, post-lock, and post-unlock questions above. The bundle names its fields
# after them.
PRECHECK = "precheck"
LOCK = "lock"
UNLOCK = "unlock"

LOCK_SELECT = "SELECT locked FROM _deploy_lock WHERE id = 1"


@dataclasses.dataclass
class LockPhaseRecord:
    """What one phase's read attempt came to, as the bundle reports it."""

    result: str = "not_run"
    attempts: int = 0
    state: str = UNKNOWN
    error: str = ""


class LockReader:
    """The lock reads for one deploy, and the record of each."""

    def __init__(
        self,
        *,
        wrangler: Sequence[str],
        db_name: str,
        wrangler_env: str,
        verify_backoff: Sequence[int],
        dry_run: bool,
    ) -> None:
        self._wrangler = tuple(wrangler)
        self._db_name = db_name
        self._wrangler_env = wrangler_env
        self._verify_backoff = tuple(verify_backoff)
        self._dry_run = dry_run
        self.last_error = ""
        self.records: dict[str, LockPhaseRecord] = {
            PRECHECK: LockPhaseRecord(),
            LOCK: LockPhaseRecord(),
            UNLOCK: LockPhaseRecord(),
        }
        # The most recent read, whichever phase it belonged to. The bundle
        # reports it alongside the per-phase rows because a failure can happen
        # between phases, and then "the last thing we knew" is the only reading
        # that exists.
        self.last = LockPhaseRecord()
        self.last_context = ""
        # Whether the most recent read found no lock table at all. `capture`
        # reports that as unlocked, which is right for a deploy and not enough
        # for a relocation (deploy_relocation.py).
        self.last_table_absent = False

    def capture(self, context: str, phase: str) -> str:
        """Read the lock for one phase. Returns "locked", "unlocked", or UNKNOWN.

        UNKNOWN is the failure value rather than a separate signal: every caller
        treats an unreadable lock exactly as it treats a lock read it does not
        expect, so one value covers the unreadable lock and the unexpected read.
        """
        state = self._read_with_retry(context)
        record = LockPhaseRecord(
            result="success" if state != UNKNOWN else "failure",
            attempts=self.last.attempts,
            state=state,
            error=self.last_error,
        )
        self.records[phase] = record
        return state

    def _read_with_retry(self, context: str) -> str:
        self.last_context = context
        self.last = LockPhaseRecord(result="in_progress")

        if self._dry_run:
            self.last = LockPhaseRecord(result="success", attempts=1, state="unlocked")
            return "unlocked"

        schedule = list(self._verify_backoff)
        attempt = 1
        while True:
            state = self._read_once()
            if state is not None:
                self.last = LockPhaseRecord(
                    result="success", attempts=attempt, state=state
                )
                return state

            if not schedule:
                self.last = LockPhaseRecord(
                    result="failure", attempts=attempt, state=UNKNOWN
                )
                return UNKNOWN

            wait = retry_sleep.sleep_seconds(
                schedule.pop(0), retry_sleep.draw_jitter(random.SystemRandom())
            )
            if self.last_error:
                deploy_reporting.emit_error(
                    f"Lock-state verification error while trying to {context}: "
                    f"{self.last_error}"
                )
            if wait > 0:
                deploy_reporting.emit_error(
                    f"Lock-state verification still ambiguous while trying to "
                    f"{context} (attempt {attempt}). Retrying in {wait}s..."
                )
                time.sleep(wait)
            attempt += 1

    def _read_once(self) -> str | None:
        """One SELECT, classified. None when the read produced no verdict."""
        result = wrangler_runner.invoke(
            [
                *self._wrangler,
                "d1",
                "execute",
                self._db_name,
                "--env",
                self._wrangler_env,
                "--remote",
                "--json",
                "--command",
                LOCK_SELECT,
            ]
        )
        try:
            verdict = lock_state.classify(
                result.returncode, result.stdout, result.stderr
            )
        except lock_state.Unreadable as gap:
            # Trailing newlines off the payload, which is wrangler's own output:
            # it goes into a log line and into a one-line bundle field, and
            # neither wants the blank line the capture ends with.
            self.last_error = str(gap).rstrip("\n")
            return None
        self.last_error = ""
        self.last_table_absent = (
            result.returncode != 0
            and lock_state.names_missing_lock_table(result.stdout)
        )
        return verdict
