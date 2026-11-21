#!/usr/bin/env python3
"""Deploy a backward-compatible schema change and promote one Worker version.

The consumer preflight establishes that the migration can run under live
traffic. The pipeline requires a single serving version, synchronizes triggers
before changing the database, and rechecks the serving version before migration
and promotion. A failed promotion can still have applied, so recovery can spend
the captured rollback target. A failed postflight leaves the new version live.
"""

from __future__ import annotations

import os
import subprocess
import sys
import traceback
from collections.abc import Sequence

import deploy_config
import deploy_diagnostics
import deploy_recovery
import deploy_reporting
import deploy_state
import deploy_versions
import wrangler_runner
from deploy_reporting import emit, emit_error
from deploy_state import DeployAbort


class Pipeline:
    """One deploy run: the order of its steps and the hooks between them."""

    def __init__(
        self,
        config: deploy_config.DeployConfig,
        *,
        reporter: deploy_reporting.Reporter,
        versions: deploy_versions.WorkerVersions,
        recovery: deploy_recovery.Recovery,
    ) -> None:
        # Only the version and recovery collaborators share mutable deploy state.
        self._config = config
        self._reporter = reporter
        self._versions = versions
        self._recovery = recovery

    def run(self) -> int:
        """Run the selected body, then the exit path. Returns the exit code."""
        exit_code = 0
        self._reporter.start_deploy()
        for line in (
            f"### Deploy to {self._config.env}",
            "| Step | Duration |",
            "|------|----------|",
        ):
            self._reporter.summary(line)

        try:
            self._run_body()
        except DeployAbort as abort:
            exit_code = abort.exit_code
        except Exception:  # noqa: BLE001 - see the comment below
            # The recovery path runs on ANY exit, and a defect in the pipeline
            # itself is an exit like any other - one that happens at the worst
            # possible time. So the traceback goes to the log and the recovery
            # layers still get to look at the state.
            traceback.print_exc()
            exit_code = 1
        return self._recovery.run(exit_code)

    def _run_body(self) -> None:
        serving_version = self._versions.capture_single_serving_version()
        self._preflight_check(serving_version)
        self._versions.verify_single_serving_version("before updating triggers")
        self._versions.update_triggers()
        self._versions.verify_single_serving_version("before the first DB mutation")

        if self._apply_migrations() != 0:
            emit("")
            emit(
                "[FAIL] Windowless migrate failed. The OLD version is still "
                "serving and NO"
            )
            emit(
                "  Time-Travel restore runs on this path - reconcile the import outcome"
            )
            emit("  and fix forward (the consumer's reconcile classification names the")
            emit("  live state and whether re-running the deploy is safe).")
            raise DeployAbort(1)

        self._versions.upload_final_version()
        self._versions.verify_final_runtime()

        self._versions.promote_final_version()

        if self._postflight_check() != 0:
            emit("")
            emit(
                "[FAIL] Windowless postflight failed AFTER the final promotion. "
                "No automatic"
            )
            emit("  recovery runs: the new version stays live (the DB moved before the")
            emit(
                "  code, so the old build has no better claim) - investigate the parity"
            )
            emit("  mismatch and fix forward.")
            raise DeployAbort(1)
        self._reporter.end_steps()

    def _preflight_check(self, serving_version: str) -> None:
        self._reporter.step(f"Pre-flight check ({self._config.env})")
        status = self._run_hook(self._config.preflight_cmd, serving_version)
        if status != 0:
            raise DeployAbort(status)

    def _apply_migrations(self) -> int:
        # The label says "migration" rather than "snapshot" deliberately: a
        # consumer whose hook applies a forward-only delta would otherwise read
        # a log line that lies about what just happened.
        self._reporter.step(f"Apply schema migration ({self._config.env})")
        return self._run_hook(self._config.migrate_cmd)

    def _postflight_check(self) -> int:
        self._reporter.step(f"Post-flight check ({self._config.env})")
        if self._config.postflight_cmd is None:
            emit("  (no POSTFLIGHT_CMD set - skipping)")
            return 0
        return self._run_hook(self._config.postflight_cmd)

    def _run_hook(self, command: Sequence[str], *arguments: str) -> int:
        """Pass the environment to every hook and the captured version to preflight.

        Streams are inherited rather than captured: a hook's output is for the
        operator, and a migration that prints progress should print it while it
        is running rather than after it finishes.

        CI is deliberately not set here. The wrangler runner scopes CI=true to
        the wrangler process it spawns, so a hook keeps whatever the caller's
        environment says.
        """
        argv = [*command, self._config.env, *arguments]
        try:
            return subprocess.run(argv, check=False).returncode
        except OSError as exc:
            emit_error(f"[FAIL] hook command failed to start: {' '.join(argv)}: {exc}")
            return 1


def build(
    config: deploy_config.DeployConfig,
) -> tuple[Pipeline, deploy_diagnostics.Diagnostics]:
    """Wire one run's collaborators together.

    Returns the diagnostics alongside the pipeline because its directory is
    claimed before the run starts, and a directory this library may not own is a
    refusal to start rather than something the exit path recovers from.
    """
    state = deploy_state.PipelineState()
    reporter = deploy_reporting.Reporter(
        github_actions=config.github_actions,
        step_summary_path=config.github_step_summary,
    )
    runner = wrangler_runner.WranglerRunner(
        wrangler=config.wrangler, backoff_seconds=config.retry_backoff
    )
    diagnostics = deploy_diagnostics.Diagnostics(
        config.diagnostics_dir, reporter=reporter
    )
    versions = deploy_versions.WorkerVersions(
        config, state, reporter=reporter, runner=runner
    )
    recovery = deploy_recovery.Recovery(
        config,
        state,
        reporter=reporter,
        runner=runner,
        diagnostics=diagnostics,
        versions=versions,
    )
    pipeline = Pipeline(
        config,
        reporter=reporter,
        versions=versions,
        recovery=recovery,
    )
    return pipeline, diagnostics


def main(argv: Sequence[str]) -> int:
    """`safe_deploy_pipeline.py <staging|production> [--sha SHA] [--expected-serving-sha SHA]`."""
    # Before anything can print: every line below carries text that may have come
    # from the API, and a stdout that refuses it aborts the deploy.
    deploy_reporting.configure_stdout()
    program = os.path.basename(sys.argv[0]) or "safe_deploy_pipeline.py"
    try:
        invocation = deploy_config.parse_args(argv)
    except deploy_config.UsageError as usage:
        if str(usage):
            emit_error(str(usage))
        emit_error(deploy_config.usage_text(program))
        return 2

    try:
        config = deploy_config.resolve(invocation, os.environ)
    except deploy_config.ConfigError as refusal:
        emit_error(f"[FAIL] {refusal}")
        return 1

    pipeline, diagnostics = build(config)
    try:
        diagnostics.prepare()
    except deploy_diagnostics.Refused as refusal:
        emit_error(f"[FAIL] {refusal}")
        return 1
    return pipeline.run()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
