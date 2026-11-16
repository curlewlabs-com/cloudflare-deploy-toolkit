"""What runs on every exit: the recovery layers, the unlock, the diagnostics.

This is the pipeline's exit path, and it runs whether the deploy succeeded or
failed. Its decision is a function of how far the run got - which is exactly what
deploy_state.PipelineState records.

CONCENTRIC LAYERS, each catching its own failure so an operator gets the
whole picture rather than the first thing that broke. The restore and the
rollback report failure the same way whether they returned it or raised it,
which is what lets the unlock below decide on the result rather than on whether
control reached it.

  1. Nothing promoted and no bookmark: exit clean, the old version still
     serving. A promote that was IN FLIGHT rolls the Worker back instead -
     either one, because either call can apply server-side and still report
     failure.
  2. A maintenance version live: roll the Worker back to the recorded previous
     version.
  3. A bookmark taken as well: restore D1 first, then roll the Worker back, then
     clear the lock.

THE UNLOCK IS NOT A LAYER. It is an obligation, and it runs on success as much
as on failure, because the windowed body's lock is released here and nowhere
else. An unlock that cannot be verified turns a green run red, since the next
deploy will refuse to start on a lock it cannot prove is clear.

It is discharged in every state but these. When the final promote may have put
real code live and the rollback did not take it back down, the lock is what
keeps writes off a Worker nobody can identify; when the D1 restore failed, it is
what keeps writes out of a database that may be half-migrated. Either way it is
left set on purpose - deploy_state.unlock_is_safe owns that decision and why the
maintenance-only case is not it.

A RELOCATION RECOVERS DIFFERENTLY, and runs none of the layers above: it takes
no bookmark and rolls nothing back. What it may do is reopen the source it
fenced, and only while deploy_relocation.source_reopen_is_owed says so. What it
tells the operator is decided last, from the state the run actually ended in,
because "locked, re-dispatch" and "complete, fix forward" are opposite
instructions and the target's unlock is settled after the source's.

The diagnostics bundle is written last, after every result field it reports has
its final value.
"""

from __future__ import annotations

import traceback

import deploy_config
import deploy_database
import deploy_diagnostics
import deploy_lock
import deploy_relocation
import deploy_reporting
import deploy_state
import deploy_versions
import wrangler_runner
from deploy_reporting import emit
from deploy_state import shell_bool


class Recovery:
    """The pipeline's exit path: recover what is recoverable, then report."""

    def __init__(
        self,
        config: deploy_config.DeployConfig,
        state: deploy_state.PipelineState,
        *,
        reporter: deploy_reporting.Reporter,
        runner: wrangler_runner.WranglerRunner,
        lock: deploy_lock.LockReader,
        diagnostics: deploy_diagnostics.Diagnostics,
        versions: deploy_versions.WorkerVersions,
        database: deploy_database.Database,
        relocation: deploy_relocation.Relocation | None = None,
    ) -> None:
        self._config = config
        self._state = state
        self._reporter = reporter
        self._runner = runner
        self._lock = lock
        self._diagnostics = diagnostics
        self._versions = versions
        self._database = database
        self._relocation = relocation

    def run(self, exit_code: int) -> int:
        """Recover, unlock, write the bundle. Returns the run's exit code."""
        state = self._state
        # Captured before the step log is closed, because closing it is what
        # forgets which step was running when the deploy stopped.
        if self._reporter.current_group:
            state.failed_step = self._reporter.current_group
        self._reporter.end_steps()

        if exit_code != 0:
            self._report_failure(exit_code)
            if self._relocation is not None:
                self._reopen_relocation_source(self._relocation)
            else:
                self._recover()

        if state.unlock_required and not state.unlock_is_safe:
            # Deliberate, and the opposite of the branch below: this is not an
            # unlock that failed, it is one that was never attempted, because
            # the lock is the last thing keeping writes off a Worker the
            # rollback could not put back. deploy_state.unlock_is_safe owns why.
            state.unlock_result = "withheld"
            if state.d1_restore_result == "failed":
                self._reporter.ci_error(
                    "Database unlock WITHHELD - the D1 restore failed, so the "
                    "schema may be part-way through the migration this deploy "
                    f"started (bookmark={state.bookmark}). The deploy lock is "
                    "left SET on purpose: it blocks application writes into that "
                    "database, and the next deploy will refuse to start on it. "
                    "Restore or reconcile the database by hand, confirm the "
                    "serving version reads it, then clear _deploy_lock through "
                    "the unlock workflow."
                )
                self._reporter.summary(
                    "| **ACTION REQUIRED** | Deploy lock left set - the D1 "
                    "restore failed; reconcile the database before clearing it |"
                )
            else:
                self._reporter.ci_error(
                    "Database unlock WITHHELD - the worker rollback did not "
                    f"restore the previous version ({state.prev_version_id or 'none'}), "
                    "so code from the failed promote may still be serving. The "
                    "deploy lock is left SET on purpose: it blocks application "
                    "writes, and the next deploy will refuse to start on it. "
                    "Confirm which version is live and reconcile the database "
                    "before clearing _deploy_lock."
                )
                self._reporter.summary(
                    "| **ACTION REQUIRED** | Deploy lock left set - confirm the "
                    "serving version before clearing it |"
                )
        elif state.unlock_required:
            if not self._database.unlock():
                self._reporter.ci_error(
                    "Database unlock remains unverified - do not retry deploy "
                    "until SELECT locked FROM _deploy_lock WHERE id = 1 returns 0."
                )
                self._reporter.summary(
                    "| **ACTION REQUIRED** | Verify _deploy_lock is clear before "
                    "retrying deploy |"
                )
                # A deploy that worked but left the lock set is not a success:
                # the next one refuses to start, and a green run would hide why.
                if exit_code == 0:
                    exit_code = 1
        elif exit_code == 0 and state.unlock_result == "not_run":
            # Only when nothing unlocked: a relocation opens its target as a
            # step of its body, and that result is the one the bundle reports.
            state.unlock_result = "not_needed"

        if exit_code != 0 and self._relocation is not None:
            # After the unlock decision above, which can still open the target.
            self._report_relocation_outcome(self._relocation)

        if not self._diagnostics.collect_wrangler_logs():
            self._reporter.ci_error("Deploy diagnostics log collection failed.")
        if not self._diagnostics.write_bundle(self._bundle_rows(exit_code)):
            self._reporter.ci_error("Deploy diagnostics bundle write failed.")

        total = self._reporter.total_seconds()
        if total is not None:
            if exit_code == 0:
                emit("")
                emit(
                    f"[OK] Deploy to {self._config.env} complete "
                    f"(sha={self._config.sha}) in {total}s"
                )
            self._reporter.summary("")
            self._reporter.summary(f"**Total: {total}s**")
        return exit_code

    def _report_failure(self, exit_code: int) -> None:
        """One error line an operator can act on, plus the run page's table."""
        config, state = self._config, self._state
        self._reporter.ci_error(
            f"Deploy failed with exit code {exit_code} (env={config.env} "
            f"sha={config.sha} bookmark={state.bookmark or 'none'} "
            f"maint_live={shell_bool(state.maint_live)} "
            f"lock_state={state.lock_state} "
            f"unlock_required={shell_bool(state.unlock_required)} "
            f"prev_version={state.prev_version_id or 'none'})"
        )
        for line in (
            "",
            "### Deploy FAILED",
            "| Key | Value |",
            "|-----|-------|",
            f"| Exit code | {exit_code} |",
            f"| Bookmark | {state.bookmark or 'none'} |",
            f"| Maintenance live | {shell_bool(state.maint_live)} |",
            f"| Lock state | {state.lock_state} |",
            f"| Unlock required | {shell_bool(state.unlock_required)} |",
            f"| Previous version | {state.prev_version_id or 'none'} |",
        ):
            self._reporter.summary(line)

    def _recover(self) -> None:
        state = self._state

        # Layer 3. A bookmark exists only after the bookmark step succeeded, so
        # its presence is the whole condition.
        if state.bookmark and state.bookmark != "null":
            self._reporter.ci_error("Recovery: D1 Time Travel restore")
            try:
                self._database.restore_from_bookmark()
            except Exception as exc:  # noqa: BLE001 - the layers are independent
                # Whatever went wrong with the restore, the Worker rollback below
                # still has to run: a database nobody could restore and a Worker
                # still serving maintenance or a failed build are independent
                # problems. The unlock is then withheld on purpose
                # (deploy_state.unlock_is_safe), so a failed restore ends with
                # the database still locked and the reason said.
                #
                # The verdict and the evidence are kept, and they are not
                # the same thing: the bundle records THAT the restore failed,
                # and this is the only place WHY survives. Continuing past a
                # failure is what makes that asymmetry easy to create - nothing
                # re-raises, so an unrecorded reason is simply gone.
                state.d1_restore_result = "failed"
                traceback.print_exc()
                self._reporter.ci_error(
                    "D1 Time Travel restore FAILED - manual intervention "
                    f"required (bookmark={state.bookmark}): {exc!r}"
                )

        # Layer 2, which layer 3 also runs: revert whenever a promoted version
        # may be the one currently serving - the maintenance version is live, or
        # either promote was in flight when the deploy failed. The maintenance
        # and final promotes can
        # apply server-side and still report failure, so the rollback is what
        # makes the outcome deterministic; it is idempotent, so a promote that
        # did not apply just re-pins the version already serving. The
        # final-promote flag is the only trigger on the windowless body, where
        # maint_live is never set.
        if not (
            state.maint_live
            or state.maint_promote_attempted
            or state.final_promote_attempted
        ):
            return
        if not state.prev_version_id:
            # A first-ever staging deploy under DEPLOY_ALLOW_STAGING_BOOTSTRAP
            # has no previous version to send traffic back to. Nothing can be
            # done automatically, so say so loudly enough for an operator to
            # finish it from the dashboard. The wording names no particular
            # version because every flag above reaches here: on the
            # windowless body maint_live is never set, so a message asserting a
            # live maintenance version would send an operator looking for
            # maintenance mode when a final promote is what may have applied.
            state.rollback_result = "manual_required"
            self._reporter.ci_error(
                "A promoted version may be serving and no previous version "
                "was captured - cannot auto-rollback. Manual intervention "
                "required."
            )
            self._reporter.summary(
                "| **ACTION REQUIRED** | Manual rollback via Cloudflare dashboard |"
            )
            return

        self._reporter.ci_error(
            f"Recovery: rolling back worker to {state.prev_version_id}"
        )
        try:
            rolled_back = self._versions.roll_back_worker()
        except Exception as exc:  # noqa: BLE001 - the layers are independent
            # Wrapped like the restore above, and for the same reason. It used
            # to be the one layer that was not, so an exception here escaped the
            # whole exit path: no unlock decision, no diagnostics bundle, and a
            # bare traceback instead of a verdict - in the incident whose
            # forensics matter most. The unlock is safe to leave to the branch
            # that owns it, because a rollback that raised did not succeed, and
            # that is exactly what makes it withhold.
            #
            # The traceback still goes to the log, because catching it is about
            # reaching the unlock decision and the bundle, not about deciding
            # the operator does not need the reason.
            rolled_back = False
            traceback.print_exc()
            self._reporter.ci_error(
                "wrangler rollback raised - treating the previous version as "
                f"NOT restored: {exc!r}"
            )
        if rolled_back:
            state.rollback_result = "success"
            return
        state.rollback_result = "failed"
        self._reporter.ci_error(
            "wrangler rollback FAILED - manual rollback required via Cloudflare "
            f"dashboard (target version: {state.prev_version_id})"
        )

    def _reopen_relocation_source(
        self, relocation: deploy_relocation.Relocation
    ) -> None:
        """Reopen the source if this run may.

        deploy_relocation.py owns who may reopen the source and why nothing here
        rolls the Worker back.
        """
        state = self._state
        if relocation.source_reopen_is_owed:
            self._reporter.ci_error(
                "Recovery: reopening the relocation source, which this run "
                "fenced and never promoted past"
            )
            try:
                reopened = relocation.reopen_source()
            except Exception as exc:  # noqa: BLE001 - the unlock decision below still runs
                # Caught like the restore and the rollback, and for the same
                # reason: the bundle and the target's unlock decision come after
                # this, and an exception here would take them with it.
                reopened = False
                state.source_unlock_result = "raised"
                traceback.print_exc()
                self._reporter.ci_error(
                    f"Reopening the relocation source raised: {exc!r}"
                )
            if not reopened:
                self._reporter.ci_error(
                    "The relocation source may still be FENCED and the old "
                    "version is serving from it, so writes are refused. Verify "
                    "SELECT locked FROM _deploy_lock WHERE id = 1 on "
                    f"{self._config.relocation_source_db} and clear it."
                )
                self._reporter.summary(
                    "| **ACTION REQUIRED** | Relocation source may still be "
                    "fenced - clear its deploy lock |"
                )

    def _report_relocation_outcome(
        self, relocation: deploy_relocation.Relocation
    ) -> None:
        """Tell the operator which state a failed relocation ended in.

        Read from what happened rather than from how far the body got: a run
        past its promote has either opened the target or it has not, and the
        instruction that is right for one is wrong for the other.
        """
        state = self._state
        if relocation.target_is_open:
            self._reporter.ci_error(
                "The relocation is COMPLETE: the target was opened and is taking "
                "writes, and the source stays fenced for good. What failed came "
                "after that. Do not re-dispatch the relocation - fix forward "
                "with an ordinary deploy."
            )
            self._reporter.summary(
                "| **ACTION REQUIRED** | Relocation complete, a later step "
                "failed - fix forward with an ordinary deploy |"
            )
            return
        if not (state.relocation_promote_reached or state.relocation_resumed):
            return
        why = (
            "it reached the promote, which can apply and still report failure"
            if state.relocation_promote_reached
            else "an earlier run fenced the source, and whether that run's "
            "promote applied is not knowable from here"
        )
        self._reporter.ci_error(
            f"The relocation source is left FENCED on purpose: {why}. The "
            "target was not opened, so the source and the target refuse writes "
            "and none is lost. Re-dispatch the relocation with the resume "
            "acknowledged (DEPLOY_RELOCATION_RESUME=true) to go forward."
        )
        self._reporter.summary(
            "| **ACTION REQUIRED** | Source and target left locked - "
            "re-dispatch the relocation, acknowledging the resume |"
        )

    def _bundle_rows(self, exit_code: int) -> list[tuple[str, str]]:
        """The diagnostics bundle's fields, in the order it has always had them.

        Every value that can carry wrangler's text goes through `compact` first:
        the bundle is parsed by splitting on lines, so a newline inside a value
        would silently invent a field.
        """
        config, state = self._config, self._state
        compact = deploy_diagnostics.compact
        rows: list[tuple[str, str]] = [
            ("failed_step", compact(state.failed_step)),
            ("exit_code", str(exit_code)),
            ("env", config.env),
            ("sha", config.sha),
            ("bookmark", state.bookmark or "none"),
            ("maintenance_live", shell_bool(state.maint_live)),
            ("lock_state", state.lock_state),
            ("unlock_required", shell_bool(state.unlock_required)),
            ("previous_version", state.prev_version_id or "none"),
            ("last_lock_context", compact(self._lock.last_context)),
            ("last_lock_attempts", str(self._lock.last.attempts)),
            ("last_lock_result", self._lock.last.result),
            ("last_lock_observed_state", self._lock.last.state),
            ("last_lock_error", compact(self._lock.last_error)),
        ]
        for prefix, phase in (
            ("precheck_lock", deploy_lock.PRECHECK),
            ("lock_acquire", deploy_lock.LOCK),
            ("unlock_verify", deploy_lock.UNLOCK),
        ):
            record = self._lock.records[phase]
            rows.extend(
                [
                    (f"{prefix}_result", record.result),
                    (f"{prefix}_attempts", str(record.attempts)),
                    (f"{prefix}_state", record.state),
                    (f"{prefix}_error", compact(record.error)),
                ]
            )
        rows.extend(
            [
                ("d1_restore_result", state.d1_restore_result),
                ("worker_rollback_result", state.rollback_result),
                ("database_unlock_result", state.unlock_result),
                ("wrangler_logs_copied", str(self._diagnostics.log_count)),
                ("total_wrangler_retries", str(self._runner.retries)),
            ]
        )
        if self._relocation is not None:
            # Appended, and only for a relocation, so every other body's bundle
            # keeps the fields and the order it has always had.
            rows.extend(
                [
                    (
                        "relocation_source_lock_state",
                        self._relocation.source_lock_state,
                    ),
                    ("relocation_resumed", shell_bool(state.relocation_resumed)),
                    (
                        "relocation_promote_reached",
                        shell_bool(state.relocation_promote_reached),
                    ),
                    ("relocation_source_unlock_result", state.source_unlock_result),
                ]
            )
        return rows
