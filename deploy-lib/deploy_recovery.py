"""Handle a failed promotion and report the deploy outcome."""

from __future__ import annotations

import traceback

import deploy_config
import deploy_diagnostics
import deploy_reporting
import deploy_state
import deploy_versions
import wrangler_runner
from deploy_reporting import emit


class Recovery:
    """The pipeline's exit path: recover what is recoverable, then report."""

    def __init__(
        self,
        config: deploy_config.DeployConfig,
        state: deploy_state.PipelineState,
        *,
        reporter: deploy_reporting.Reporter,
        runner: wrangler_runner.WranglerRunner,
        diagnostics: deploy_diagnostics.Diagnostics,
        versions: deploy_versions.WorkerVersions,
    ) -> None:
        self._config = config
        self._state = state
        self._reporter = reporter
        self._runner = runner
        self._diagnostics = diagnostics
        self._versions = versions

    def run(self, exit_code: int) -> int:
        """Recover and write the diagnostics bundle. Returns the run's exit code."""
        state = self._state
        # Captured before the step log is closed, because closing it is what
        # forgets which step was running when the deploy stopped.
        if self._reporter.current_group:
            state.failed_step = self._reporter.current_group
        self._reporter.end_steps()

        if exit_code != 0:
            self._report_failure(exit_code)
            self._recover()

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
            f"sha={config.sha} failed_step={state.failed_step} "
            f"prev_version={state.prev_version_id or 'none'})"
        )
        for line in (
            "",
            "### Deploy FAILED",
            "| Key | Value |",
            "|-----|-------|",
            f"| Exit code | {exit_code} |",
            f"| Previous version | {state.prev_version_id or 'none'} |",
        ):
            self._reporter.summary(line)

    def _recover(self) -> None:
        state = self._state

        # A failed submission can have applied. A failure before submission or
        # after a successful promotion has no rollback obligation.
        if not state.final_promote_attempted:
            return
        if not state.prev_version_id:
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
        except Exception as exc:  # noqa: BLE001 - diagnostics must survive rollback failure
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

    def _bundle_rows(self, exit_code: int) -> list[tuple[str, str]]:
        """The diagnostics bundle's fields, in the order it has always had them.

        Every value that can carry wrangler's text goes through `compact` first:
        the bundle is parsed by splitting on lines, so a newline inside a value
        would silently invent a field.
        """
        config, state = self._config, self._state
        compact = deploy_diagnostics.compact
        return [
            ("failed_step", compact(state.failed_step)),
            ("exit_code", str(exit_code)),
            ("env", config.env),
            ("sha", config.sha),
            ("previous_version", state.prev_version_id or "none"),
            ("worker_rollback_result", state.rollback_result),
            ("wrangler_logs_copied", str(self._diagnostics.log_count)),
            ("total_wrangler_retries", str(self._runner.retries)),
        ]
