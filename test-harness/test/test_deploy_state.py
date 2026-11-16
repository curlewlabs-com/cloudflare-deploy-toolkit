#!/usr/bin/env python3
"""Suite for the one decision the pipeline's state record makes on its own.

`unlock_is_safe` decides whether the recovery path clears the deploy lock. The
default is yes, because a lock left set refuses the next deploy and the product
stays down until someone clears it by hand. The exceptions are the states where
clearing it is worse than that. One: the final promote may have put real code live,
the rollback did not take it back down, and the D1 restore has already moved the
schema underneath it. Unlocking there admits writes from new code against an old
schema. The other is a D1 restore that failed, which may leave the database
half-migrated whatever the rollback put back.

The cases below are the truth table, written out rather than generated, because
each row is a different operational story and the wrong verdict on any of them
is a production incident rather than a failed test.
"""

from __future__ import annotations

import pathlib
import sys
import unittest

MODULE = pathlib.Path(__file__).resolve().parents[2] / "deploy-lib" / "deploy_state.py"

sys.path.insert(0, str(MODULE.parent))

import deploy_state


def state(**fields: object) -> deploy_state.PipelineState:
    record = deploy_state.PipelineState()
    for name, value in fields.items():
        setattr(record, name, value)
    return record


class UnlockIsSafe(unittest.TestCase):
    def test_a_run_that_never_reached_the_final_promote_unlocks(self) -> None:
        # Layer 1 and layer 2 shapes: nothing promoted, or only the maintenance
        # version. The lock is owed and nothing argues for keeping it.
        for record in (
            state(unlock_required=True),
            state(unlock_required=True, maint_live=True),
            state(unlock_required=True, maint_promote_attempted=True),
        ):
            with self.subTest(record=record):
                self.assertTrue(record.unlock_is_safe)

    def test_a_failed_rollback_with_only_maintenance_live_still_unlocks(self) -> None:
        # The case that separates this from "unlock only on a successful
        # rollback". The maintenance version returns 503 unconditionally, so no
        # write can land whatever is serving - holding the lock would block the
        # next deploy and protect nothing.
        self.assertTrue(
            state(
                unlock_required=True,
                maint_live=True,
                rollback_result="failed",
            ).unlock_is_safe
        )

    def test_a_failed_rollback_after_the_final_promote_withholds(self) -> None:
        # The incident this exists for: the promote may have applied
        # server-side, the rollback did not put the previous version back, and
        # the restore has already taken the schema back. New code, old schema -
        # the lock is the last thing between that and live writes.
        for result in ("failed", "manual_required", "not_needed"):
            with self.subTest(rollback_result=result):
                self.assertFalse(
                    state(
                        unlock_required=True,
                        maint_live=True,
                        final_promote_attempted=True,
                        rollback_result=result,
                    ).unlock_is_safe
                )

    def test_a_successful_rollback_after_the_final_promote_unlocks(self) -> None:
        # The rollback is what makes the serving version known again, so the
        # reason to withhold is gone.
        self.assertTrue(
            state(
                unlock_required=True,
                final_promote_attempted=True,
                rollback_result="success",
            ).unlock_is_safe
        )

    def test_a_failed_restore_withholds_whatever_the_rollback_did(self) -> None:
        # The migrate ran and the restore that should have undone it failed, so
        # the database may be half-migrated. Old code put back by a successful
        # rollback would write into it as surely as new code would, and the
        # final promote need not have been attempted at all.
        for fields in (
            {"maint_live": True, "rollback_result": "success"},
            {"maint_live": True, "rollback_result": "failed"},
            {"final_promote_attempted": True, "rollback_result": "success"},
        ):
            with self.subTest(**fields):
                self.assertFalse(
                    state(
                        unlock_required=True, d1_restore_result="failed", **fields
                    ).unlock_is_safe
                )

    def test_a_green_run_can_never_withhold(self) -> None:
        # The flag is cleared when the promote succeeds, and rollback_result is
        # only written by the recovery path, which runs on failure. A deploy
        # that worked must never leave its own lock set.
        self.assertTrue(state(unlock_required=True).unlock_is_safe)


if __name__ == "__main__":
    unittest.main()
