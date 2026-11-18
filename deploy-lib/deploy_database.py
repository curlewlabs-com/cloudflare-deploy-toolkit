"""What the deploy does to D1: the lock, the bookmark, and the restore.

Every step here writes one of the database fields on the run's state - the lock
state, whether an unlock is owed, the Time Travel bookmark, and the results the
diagnostics bundle reports for unlock and restore. Those results belong to the
recovery path, which is why they live here rather than beside the layers that
call them.

THE LOCK IS NEVER TRUSTED FROM AN EXIT STATUS. Every mutation is followed by a
read-back, and the read-back is what decides: Cloudflare can apply a write and
still report failure, and it can report success on a write that a later read
contradicts. What one read means is deploy_lock.py, and the answer it will not
give - unreadable - is the one recovery plans for, because "unlock still owed" has to
survive a lock nobody could read.

The windowless body touches none of this: no lock, no drain, no bookmark, and
therefore no restore.
"""

from __future__ import annotations

import json
import time
from typing import TypeGuard

import deploy_config
import deploy_lock
import deploy_reporting
import deploy_state
import wrangler_runner
from deploy_reporting import emit, emit_error
from deploy_state import DeployAbort

# The lock table's DDL, which MUST stay in sync with
# worker-patterns/deploy-lock.sql. Inlined rather than read at runtime so the
# library depends on no file but the modules beside it; change the SQL file and this copy in the same
# commit.
LOCK_DDL = (
    "CREATE TABLE IF NOT EXISTS _deploy_lock (id INTEGER PRIMARY KEY "
    "CHECK (id = 1), locked INTEGER NOT NULL DEFAULT 0) STRICT; "
    "INSERT OR IGNORE INTO _deploy_lock (id, locked) VALUES (1, 1); "
    "UPDATE _deploy_lock SET locked = 1 WHERE id = 1"
)
UNLOCK_SQL = "UPDATE _deploy_lock SET locked = 0 WHERE id = 1"


def _is_json_object(value: object) -> TypeGuard[dict[str, object]]:
    """True for a decoded JSON object. Its keys are strings, which `isinstance`
    alone cannot tell a type checker."""
    return isinstance(value, dict)


def bookmark_from(document: str) -> str:
    """The bookmark in a `d1 time-travel info --json` document, or "" for none.

    A non-empty string or nothing. Every other shape - a null, a number, a
    document that is not an object - is one nobody modeled, and the caller
    refuses the deploy rather than opening a maintenance window it cannot
    restore out of.
    """
    try:
        parsed: object = json.loads(document)
    except json.JSONDecodeError:
        return ""
    if not _is_json_object(parsed):
        return ""
    bookmark = parsed.get("bookmark")
    return bookmark if isinstance(bookmark, str) else ""


class Database:
    """The D1 half of a deploy: the lock, the drain point, the bookmark, the restore."""

    def __init__(
        self,
        config: deploy_config.DeployConfig,
        state: deploy_state.PipelineState,
        *,
        reporter: deploy_reporting.Reporter,
        runner: wrangler_runner.WranglerRunner,
        lock: deploy_lock.LockReader,
        role: str = "database",
    ) -> None:
        self._config = config
        self._state = state
        self._reporter = reporter
        self._runner = runner
        self._lock = lock
        # What the step labels call this database. A relocation has a source
        # and a target, and a log that says "Unlock database" for each would
        # not say which one was opened.
        self._role = role

    def verify_lock_is_clear(self) -> None:
        """Refuse to start on a lock somebody else holds.

        The windowed and windowless bodies run this. A set lock means another deploy is in flight or
        crashed mid-window, and neither body may run on top of that. Read-only -
        only the windowed body ever acquires the lock.
        """
        config, state = self._config, self._state
        self._reporter.step(f"Verify deploy lock is clear ({config.env})")
        state.lock_state = self._lock.capture(
            "verify the deploy lock is clear before mutating remote state",
            deploy_lock.PRECHECK,
        )
        if state.lock_state == "unlocked":
            emit("Deploy lock state before deploy: unlocked")
            return
        if state.lock_state == "locked":
            emit_error("[FAIL] Refusing to deploy because _deploy_lock is already set.")
            emit_error(
                "  Clear the existing lock and verify SELECT locked FROM "
                "_deploy_lock WHERE id = 1 returns 0 before retrying."
            )
            raise DeployAbort(1)
        emit_error(
            "[FAIL] Could not verify whether _deploy_lock is clear before "
            "starting the deploy."
        )
        raise DeployAbort(1)

    def lock(self) -> None:
        config, state = self._config, self._state
        self._reporter.step(f"Lock {self._role} ({config.env})")
        # The CREATE / INSERT OR IGNORE / UPDATE triple is idempotent, so
        # retrying a classified-transient failure converges on the same locked
        # row. If a retry still fails, the read-back below is what decides.
        result = self._execute("lock_mutate", LOCK_DDL)
        state.lock_state = self._lock.capture(
            "verify the database lock after the lock attempt", deploy_lock.LOCK
        )

        if state.lock_state == "locked":
            state.unlock_required = True
            if not result.ok:
                emit_error(
                    "[FAIL] Lock command returned non-zero even though read-back "
                    "confirmed _deploy_lock.locked = 1. Aborting so cleanup can "
                    "roll back and clear the verified lock."
                )
                raise DeployAbort(1)
            return
        if state.lock_state == "unlocked":
            state.unlock_required = False
            emit_error(
                "[FAIL] Lock attempt did not leave _deploy_lock in the locked state."
            )
            raise DeployAbort(1)
        # Unreadable. The unlock is still owed - a lock nobody could read is not
        # a lock nobody took.
        state.unlock_required = True
        emit_error(
            "[FAIL] Could not verify whether the database lock mutation applied."
        )
        raise DeployAbort(1)

    def drain_requests(self) -> None:
        """Wait for the old isolate's in-flight requests to finish.

        Consumers set DEPLOY_DRAIN_SECONDS above their enforced worst-case total
        request runtime; worker-patterns/README.md owns the request-bound
        checklist.
        """
        config = self._config
        self._reporter.step("Drain in-flight requests")
        emit(
            f"Waiting {config.drain_seconds} seconds for old isolate requests "
            "to complete..."
        )
        time.sleep(config.drain_seconds)

    def save_bookmark(self) -> None:
        config, state = self._config, self._state
        self._reporter.step(f"Save D1 Time Travel bookmark ({config.env})")
        # A pure read, so retrying is always safe.
        result = self._runner.run(
            "bookmark_fetch",
            ["d1", "time-travel", "info", config.db_name, "--json"],
        )
        if not result.ok:
            raise DeployAbort(result.returncode)

        state.bookmark = bookmark_from(result.stdout)
        if not state.bookmark:
            emit_error("[FAIL] Failed to capture Time Travel bookmark. Aborting.")
            raise DeployAbort(1)
        # A bookmark that cannot be passed back to wrangler is not a restore
        # point, and this step is what the window's contract rests on: it runs
        # before the migration, so refusing here means the window never opens
        # without a restore point to close it with. Proceeding would migrate
        # with no way back and say nothing until recovery needed it. The
        # recovery itself survives either way - deploy_recovery catches a failed
        # restore, still runs the rollback, and then withholds the unlock - so
        # this is about not spending the window, not about keeping the recovery
        # path alive.
        if not wrangler_runner.reaches_wrangler(state.bookmark):
            emit_error(
                "[FAIL] Time Travel bookmark cannot be passed back to wrangler. "
                "Aborting."
            )
            raise DeployAbort(1)

        emit(f"Bookmark: {state.bookmark}")
        self._reporter.summary(f"| Bookmark | `{state.bookmark}` |")

    # --- recovery ----------------------------------------------------------

    def unlock(self) -> bool:
        """Clear the lock and verify it. False when the unlock stays unproven.

        On the windowed body this is called only by the recovery path, on
        success as much as on failure: that body's lock is released there and
        nowhere else, so one code path clears it however the deploy ended. A
        relocation also calls it as a step (deploy_relocation.py), to open its
        target.
        """
        config, state = self._config, self._state
        self._reporter.step(f"Unlock {self._role} ({config.env})")
        # The UPDATE is idempotent, so retrying a classified-transient failure
        # is safe; if a retry still fails, the read-back below is what decides.
        result = self._execute("unlock_mutate", UNLOCK_SQL)
        state.lock_state = self._lock.capture(
            "verify the database unlock after the unlock attempt", deploy_lock.UNLOCK
        )

        if state.lock_state == "unlocked":
            state.unlock_required = False
            state.unlock_result = "success"
            if not result.ok:
                emit_error(
                    "Unlock command returned non-zero, but read-back confirmed "
                    "_deploy_lock.locked = 0. Continuing with the verified "
                    "remote state."
                )
            self._reporter.end_steps()
            return True
        if state.lock_state == "locked":
            state.unlock_required = True
            state.unlock_result = "failed_locked"
            emit_error("[FAIL] Unlock verification says _deploy_lock is still set.")
            self._reporter.end_steps()
            return False
        state.unlock_required = True
        state.unlock_result = "unverified"
        emit_error("[FAIL] Could not verify whether the database unlock succeeded.")
        self._reporter.end_steps()
        return False

    def restore_from_bookmark(self) -> None:
        """Restore D1 to the bookmark taken before the migration.

        Worker recovery is separate and runs after this: a rollback to the
        previous version, rather than re-deploying current code, which may
        itself be the cause of the failure.
        """
        config, state = self._config, self._state
        self._reporter.step(f"ROLLBACK: restoring Time Travel bookmark ({config.env})")
        emit(
            "Schema check or migration failed! Rolling back using bookmark: "
            f"{state.bookmark}"
        )
        # Idempotent at a given bookmark, so retrying a classified-transient
        # failure converges on the same post-restore state.
        result = self._runner.run(
            "tt_restore",
            [
                "d1",
                "time-travel",
                "restore",
                config.db_name,
                f"--bookmark={state.bookmark}",
            ],
        )
        wrangler_runner.write_stdout(result.stdout)
        if not result.ok:
            raise DeployAbort(result.returncode)
        state.d1_restore_result = "success"
        emit("D1 rollback complete.")

    # --- helpers -----------------------------------------------------------

    def _execute(self, label: str, command: str) -> wrangler_runner.WranglerResult:
        """A remote `d1 execute` whose exit status is advisory, not the verdict."""
        result = self._runner.run(
            label,
            [
                "d1",
                "execute",
                self._config.db_name,
                "--env",
                self._config.wrangler_env,
                "--remote",
                "--command",
                command,
            ],
        )
        wrangler_runner.write_stdout(result.stdout)
        return result

    def _wrangler_text(self) -> str:
        return " ".join(self._config.wrangler)
