#!/usr/bin/env python3
"""The safe-deploy pipeline for a stateful Cloudflare Worker.

Runs what `wrangler deploy` should but does not: the Cloudflare operations a deploy actually performs: script upload, version
promotion, and schedules update
in an order where the one that fails most often - the schedules update, whose
cron limit once took a production Worker down - lands before anything is
serving, and an exit path that knows how to put the world back.

The bodies, chosen by DEPLOY_PIPELINE_MODE. The choice is explicit config and is
never inferred from the change being deployed; an unrecognized value refuses to
run rather than picking one.

  Windowed (the default): preflight -> capture the rollback target -> upload a
  maintenance version -> check its runtime -> update triggers -> PROMOTE the
  maintenance version -> lock the database -> drain -> bookmark -> migrate ->
  postflight -> upload the final version -> promote it -> unlock. Everything
  after the promote is inside the window, so a failure there gets the full
  recovery.

  Windowless: preflight -> capture the SINGLE serving version -> update triggers
  -> re-verify the serving version -> migrate -> upload final -> check its
  runtime -> re-verify -> promote final -> postflight. No maintenance version,
  no lock, no bookmark, and therefore no D1 restore: recovery is fix-forward,
  and the consumer's preflight hook owns proving the migration is safe to run
  under live traffic.

  Relocation: preflight -> read source and target locks -> capture the SINGLE
  serving version -> update triggers -> upload final -> check its runtime ->
  FENCE THE SOURCE -> drain -> copy hook -> migrate -> read each lock back -> re-verify
  -> promote final -> verify it is what serves -> OPEN THE TARGET -> postflight.
  Moves the Worker off a source database onto the one this checkout deploys
  against. No maintenance version and no rollback; what recovery does is decided
  by those locks, which deploy_relocation.py owns.

  A runtime check runs only when the consumer sets DEPLOY_EXPECTED_RUNTIME, and
  each runs before a window or a write pause opens
  (deploy_versions.WorkerVersions.verify_maintenance_runtime says why the
  windowed body reads its first upload rather than its last).

This file is the orchestration and nothing else. The steps belong to the state they mutate - deploy_versions.py for what the
Worker is serving, deploy_database.py for what D1 is doing - and the exit path is
deploy_recovery.py. What is left here is what owns no state of its own: the
order of the steps, the consumer hooks, and the wiring.

THE HOOK CONTRACT IS COMMANDS. This pipeline is a program a consumer's wrapper
starts rather than a library it sources, so the hooks are commands named by
PREFLIGHT_CMD, MIGRATE_CMD, and POSTFLIGHT_CMD (and RELOCATE_COPY_CMD on the
relocation body), each run as a subprocess with the
environment name appended as its last argument. The consequence to design for is
that a hook cannot reach into the pipeline's state; the state the exit path reads
is deploy_state.PipelineState, and nothing outside this process can write it.

See deploy-lib/README.md for the config variables, the hook contract, and the
recovery table.
"""

from __future__ import annotations

import contextlib
import dataclasses
import functools
import os
import signal
import subprocess
import sys
import time
import traceback
from collections.abc import Callable, Iterator, Sequence
from types import FrameType

import deploy_config
import deploy_database
import deploy_diagnostics
import deploy_lock
import deploy_recovery
import deploy_relocation
import deploy_reporting
import deploy_state
import deploy_versions
import wrangler_runner
from deploy_reporting import emit, emit_error
from deploy_state import DeployAbort

# What a hook stopped at its deadline returns, as timeout(1) does.
HOOK_TIMED_OUT = 124
# How long a stopped hook gets to clean up after SIGTERM before SIGKILL. A copy
# hook holds user data in working files and removes them on the way out.
HOOK_STOP_GRACE_SECONDS = 10
HOOK_STOP_POLL_SECONDS = 0.1
# The signals that end the pipeline from outside: a CI cancellation, a closed
# terminal, an interrupt.
TERMINATION_SIGNALS = (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)


class Pipeline:
    """One deploy run: the order of its steps and the hooks between them."""

    def __init__(
        self,
        config: deploy_config.DeployConfig,
        *,
        reporter: deploy_reporting.Reporter,
        versions: deploy_versions.WorkerVersions,
        database: deploy_database.Database,
        recovery: deploy_recovery.Recovery,
        relocation: deploy_relocation.Relocation | None = None,
    ) -> None:
        # No PipelineState here, deliberately: the orchestrator decides the
        # order of the steps and nothing else, so the run's state belongs to the version,
        # database, and recovery collaborators that write and read it.
        self._config = config
        self._reporter = reporter
        self._versions = versions
        self._database = database
        self._recovery = recovery
        self._relocation = relocation

    def run(self) -> int:
        """Run the selected body, then the exit path. Returns the exit code."""
        exit_code = 0
        self._reporter.start_deploy()
        for line in (
            f"### Deploy to {self._config.env}",
            "| Step | Duration |",
            "|------|----------|",
            f"| Pipeline mode | `{self._config.mode}` |",
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
        self._preflight_check()
        if self._config.mode == deploy_config.RELOCATION:
            # Not verify_lock_is_clear: a relocation's target is locked whenever
            # it holds data, and its source is locked whenever an earlier run
            # fenced it. Which of those states may be run from is the body's
            # first step.
            emit("-> Relocation: moving the Worker onto the target database.")
            self._relocation_body()
            return
        self._database.verify_lock_is_clear()
        if self._config.mode == deploy_config.WINDOWLESS:
            self._versions.capture_single_serving_version()
            emit("-> Windowless deploy: no maintenance window, no lock, no bookmark.")
            self._windowless_body()
        else:
            self._versions.capture_previous_version_id()
            emit("-> Windowed deploy: full maintenance-window sequence.")
            self._windowed_body()

    def _windowed_body(self) -> None:
        """The full maintenance-window sequence.

        A reliability primitive: it takes the lock, drains, bookmarks, and its
        post-bookmark failures trigger the full recovery path. Do not weaken
        it; the windowless body is an alternative alongside it, never a
        replacement.
        """
        self._versions.upload_maintenance_version()
        self._versions.verify_maintenance_runtime()
        self._versions.update_triggers()

        # ----------- Point of no return -----------
        # Updating the triggers is the last step that can fail with nothing
        # live. Once the maintenance version is promoted the Worker is serving
        # maintenance mode, and every failure after this line has to recover.

        self._versions.promote_maintenance_version()
        self._database.lock()
        self._database.drain_requests()
        self._database.save_bookmark()

        # From here a failure means the D1 restore, so the migrate and
        # postflight statuses are inspected rather than allowed to propagate.
        # The exit code the recovery path sees is 1 either way; the message
        # below is what tells an operator which recovery is about to run.
        status = self._apply_migrations()
        if status == 0:
            status = self._postflight_check()
        if status != 0:
            emit("")
            emit(
                "[FAIL] Migration or postflight check failed. "
                "The recovery path will run D1 restore + worker rollback."
            )
            raise DeployAbort(1)

        self._versions.upload_final_version()
        self._versions.promote_final_version()
        self._reporter.end_steps()

    def _windowless_body(self) -> None:
        """No maintenance version, no lock flip, no drain, no bookmark, no restore.

        The order is deliberate. Triggers sync first, before any DB mutation, so
        a cron-limit failure aborts with nothing else changed and a consumer
        migrate hook that records config-coupled state still lands after the
        trigger push - the same relative order the windowed body has. The
        migrate runs strictly before the final promote (schema, then code), and
        the postflight after it, verifying the final serving world; by then the
        promote flag is cleared, so a postflight failure is a loud red with no
        automatic recovery.
        """
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
        self._versions.verify_single_serving_version("before the final promotion")
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

    def _relocation_body(self) -> None:
        """Fence the source, drain it, copy it, promote onto the target, open
        the target.

        A reliability primitive, like the windowed body: its order is what makes
        those locks a complete record. Everything that can fail without a
        pause - the lock reads, the trigger sync, the upload of the target-bound
        version - runs before the fence, so the write pause holds only the copy,
        the migrate, and the promote. The copy hook must leave the target an
        exact copy that is STILL LOCKED; this body opens it, last, and only once
        the promoted version is verified to be the one serving, because a target
        opened under a version that does not read it records a relocation as
        complete while every write is still refused.
        """
        relocation = self._relocation
        if relocation is None:
            raise RuntimeError("the relocation body was selected without its wiring")
        relocation.read_start_state()
        self._versions.capture_single_serving_version()
        self._versions.update_triggers()
        self._versions.upload_final_version()
        self._versions.verify_final_runtime()

        # ----------- The write pause starts here -----------
        # A failure from here to the promote reopens the source, if this run
        # is the one that fenced it (deploy_relocation.source_reopen_is_owed).
        relocation.fence_source()
        # The fence refuses requests from here on, not the ones already past
        # it: a request admitted just before the lock flipped can still write
        # to the source, and a write landing after the copy read the source is
        # in neither the copy nor its proof, so the promote would leave it
        # behind. The drain is the windowed body's, for the same requests. A
        # resumed run drains too: the lock records neither when it was set nor
        # whether the run that set it drained, and that run may have died the
        # moment after fencing.
        self._database.drain_requests()

        # Each fenced hook runs against the deadline the fence started: a copy
        # that hangs must not hold production's writes for as long as it likes.
        self._reporter.step(f"Copy the source into the target ({self._config.env})")
        copy_cmd = self._config.relocate_copy_cmd
        if copy_cmd is None or self._run_hook(copy_cmd, fenced=True) != 0:
            emit("")
            emit(
                "[FAIL] The relocation's copy did not complete and prove itself exact."
            )
            raise DeployAbort(1)

        if self._apply_migrations(fenced=True) != 0:
            emit("")
            emit("[FAIL] The migrate hook failed against the relocation's target.")
            raise DeployAbort(1)

        # The hooks are trusted for what only they can know - that the copy is
        # exact - and for nothing a read can settle.
        relocation.verify_still_closed()
        self._versions.verify_single_serving_version(
            "before the relocation's promotion"
        )
        relocation.refuse_if_the_pause_is_spent()

        # ----------- Point of no return -----------
        # From here the source is never reopened and nothing is rolled back. A
        # failure before the target opens leaves source and target locked, and
        # re-dispatching the relocation goes forward; the target opens only by
        # the step below that says so.
        relocation.mark_promote_reached()
        self._versions.promote_final_version()
        self._versions.verify_final_version_is_serving()
        relocation.open_target()

        if self._postflight_check() != 0:
            emit("")
            emit(
                "[FAIL] Relocation postflight failed AFTER the target was opened. "
                "The relocation"
            )
            emit("  is complete and the target is taking writes - investigate and fix")
            emit("  forward with an ordinary deploy.")
            raise DeployAbort(1)
        self._reporter.end_steps()

    # --- consumer hooks ----------------------------------------------------

    def _preflight_check(self) -> None:
        self._reporter.step(f"Pre-flight check ({self._config.env})")
        if self._config.preflight_cmd is None:
            emit("  (no PREFLIGHT_CMD set - skipping)")
            return
        status = self._run_hook(self._config.preflight_cmd)
        if status != 0:
            raise DeployAbort(status)

    def _apply_migrations(self, *, fenced: bool = False) -> int:
        # The label says "migration" rather than "snapshot" deliberately: a
        # consumer whose hook applies a forward-only delta would otherwise read
        # a log line that lies about what just happened.
        self._reporter.step(f"Apply schema migration ({self._config.env})")
        return self._run_hook(self._config.migrate_cmd, fenced=fenced)

    def _postflight_check(self) -> int:
        self._reporter.step(f"Post-flight check ({self._config.env})")
        if self._config.postflight_cmd is None:
            emit("  (no POSTFLIGHT_CMD set - skipping)")
            return 0
        return self._run_hook(self._config.postflight_cmd)

    def _run_hook(self, command: Sequence[str], *, fenced: bool = False) -> int:
        """Run a consumer hook with the environment name as its last argument.

        Streams are inherited rather than captured: a hook's output is for the
        operator, and a migration that prints progress should print it while it
        is running rather than after it finishes.

        CI is deliberately not set here. The wrangler runner scopes CI=true to
        the wrangler process it spawns, so a hook keeps whatever the caller's
        environment says.

        `fenced` runs the hook against the relocation's pause deadline
        (_run_until), which every other hook has none of.
        """
        argv = [*command, self._config.env]
        deadline = self._relocation.pause_deadline if self._relocation else None
        if fenced and deadline is not None:
            return _run_until(argv, deadline)
        try:
            return subprocess.run(argv, check=False).returncode
        except OSError as exc:
            emit_error(f"[FAIL] hook command failed to start: {' '.join(argv)}: {exc}")
            return 1


def _run_until(argv: Sequence[str], deadline: float) -> int:
    """Run a hook, and stop it and everything it started at `deadline`.

    In a process group of its own, because a hook is a shell script that starts
    the program doing the work, and stopping only the shell would leave that
    program running against the databases after the pipeline had given the
    source back. SIGTERM first: the hook gets HOOK_STOP_GRACE_SECONDS to remove
    what it must not leave behind, then SIGKILL - to the GROUP, and decided by
    whether the group is gone rather than by whether the shell is. The shell
    exits on SIGTERM at once; what it started may be ignoring it.

    The group of its own has a cost this has to pay back: a signal sent to the
    pipeline's group no longer reaches the hook. So while the hook runs, a
    signal that ends the pipeline stops the hook's group first
    (_stopping_the_hook_if_the_pipeline_is_ended).
    """
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        emit_error(f"[FAIL] pause budget spent before {argv[0]} could start")
        return HOOK_TIMED_OUT
    with _stopping_the_hook_if_the_pipeline_is_ended(argv) as hook:
        if hook is None:
            return 1
        try:
            return hook.wait(remaining)
        except subprocess.TimeoutExpired:
            emit_error(
                f"[FAIL] {' '.join(argv)} ran past the relocation's pause budget - "
                "stopping it"
            )
        _stop_group(hook.process)
    return HOOK_TIMED_OUT


def _stop_group(process: subprocess.Popen[bytes]) -> None:
    """SIGTERM the hook's group, wait out the grace period, SIGKILL what is left."""
    group = process.pid
    _signal_group(group, signal.SIGTERM)
    give_up = time.monotonic() + HOOK_STOP_GRACE_SECONDS
    while _group_has_members(process, group) and time.monotonic() < give_up:
        time.sleep(HOOK_STOP_POLL_SECONDS)
    if _group_has_members(process, group):
        _signal_group(group, signal.SIGKILL)
    process.wait()


class _HookWaitInterrupted(BaseException):
    """Raised by the cancellation handler to end the wait on a fenced hook.

    Only while that wait is under way, and `_FencedHook.wait` catches it, so it
    never reaches a caller.
    """


@dataclasses.dataclass(frozen=True)
class _FencedHook:
    """A fenced hook that started, as the cancellation guard yields it."""

    process: subprocess.Popen[bytes]
    # `process.wait`, except that a signal ending the pipeline stops the hook's
    # group before the signal is delivered again.
    wait: Callable[[float], int]


@contextlib.contextmanager
def _stopping_the_hook_if_the_pipeline_is_ended(
    argv: Sequence[str],
) -> Iterator[_FencedHook | None]:
    """Own cancellation before a fenced hook can start running independently.

    Before hooks had a group of their own, a cancellation reached the hook
    because it shared the pipeline's. Without this it would outlive the
    pipeline: a copy still running with nobody enforcing the pause budget and no
    recovery to follow. The pipeline then ends exactly as it would have - the
    previous dispositions are restored and the signal delivered again.

    The handler records the signal and does nothing else, apart from breaking
    the wait on the hook. Python runs a handler between the steps of whatever it
    interrupted, and the stop cannot run under what such a step can hold: a
    `print` mid-flush holds stderr's buffered writer, which the [STOP] line
    would re-enter with a RuntimeError, and a `Popen.wait` holds the lock the
    stop's own reap would block on for good. So the stop runs on the pipeline's
    own path.
    """
    previous = {signum: signal.getsignal(signum) for signum in TERMINATION_SIGNALS}
    process: subprocess.Popen[bytes] | None = None
    waiting = False
    stopping = False
    pending: int | None = None

    def restore_handlers() -> None:
        for signum, handler in previous.items():
            if signal.getsignal(signum) is stop_then_end:
                signal.signal(signum, handler)

    def finish_cancellation() -> None:
        nonlocal stopping
        if stopping or pending is None:
            return
        stopping = True
        emit_error(f"[STOP] signal {pending}: stopping fenced hook {' '.join(argv)}")
        if process is not None:
            _stop_group(process)
        restore_handlers()
        os.kill(os.getpid(), pending)

    def stop_then_end(signum: int, _frame: FrameType | None) -> None:
        nonlocal pending, waiting
        if pending is None:
            pending = signum
        # Raising anywhere but the wait could abandon a child Popen has created
        # without returning its handle, or cut a stop sequence short. Cleared
        # first, so a second signal during the unwind is only recorded.
        if waiting:
            waiting = False
            raise _HookWaitInterrupted

    def wait(started: subprocess.Popen[bytes], timeout: float) -> int:
        nonlocal waiting
        # The flag is set inside the outer try, and cleared before it ends, so
        # every raise the handler makes lands in the except below.
        try:
            try:
                waiting = True
                if pending is None:
                    return started.wait(timeout=timeout)
            finally:
                waiting = False
        except _HookWaitInterrupted:
            pass
        finish_cancellation()
        # Reached only when the signal, delivered again, did not end the
        # pipeline: its disposition ignores it. The stopped hook then reports
        # like any other that failed.
        return started.wait()

    try:
        for signum in previous:
            signal.signal(signum, stop_then_end)
        try:
            process = subprocess.Popen(argv, start_new_session=True)
        except OSError as exc:
            # Startup errors must not hide a failure in the later stop sequence.
            emit_error(f"[FAIL] hook command failed to start: {' '.join(argv)}: {exc}")
        finish_cancellation()
        if process is None:
            yield None
        else:
            yield _FencedHook(process, functools.partial(wait, process))
    finally:
        # A failed spawn still owes a signal deferred during its setup, and so
        # does one that landed outside the wait. A signal arriving while this
        # runs is only recorded, so cleanup cannot be cut short; one recorded
        # after the first check but before the restore is still delivered.
        try:
            finish_cancellation()
        finally:
            restore_handlers()
            finish_cancellation()


def _signal_group(group: int, stop: signal.Signals) -> None:
    # Already gone is the outcome the signal was for, and _group_has_members
    # says why a permission error means the same here. The toolkit ships as a
    # standalone or vendored copy (its README), so it carries its own stop
    # rather than depending on a consumer's process helpers.
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(group, stop)


def _group_has_members(process: subprocess.Popen[bytes], group: int) -> bool:
    """Whether anything in the hook's process group is still running.

    The hook's own shell is reaped first: an exited child that nobody has
    waited on still counts as a member of its group, and would make a group
    that is in fact empty look alive for the whole grace period.
    """
    process.poll()
    try:
        os.killpg(group, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Every member is this process's own descendant, so there is none it
        # lacks permission to signal. macOS answers this way for a group whose
        # only members left are exited and not yet reaped by whoever inherited
        # them: nothing there is running, and nothing there can be stopped.
        return False
    return True


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
    lock = deploy_lock.LockReader(
        wrangler=config.wrangler,
        db_name=config.db_name,
        wrangler_env=config.wrangler_env,
        verify_backoff=config.verify_backoff,
    )
    diagnostics = deploy_diagnostics.Diagnostics(
        config.diagnostics_dir, reporter=reporter
    )
    versions = deploy_versions.WorkerVersions(
        config, state, reporter=reporter, runner=runner
    )
    relocating = config.mode == deploy_config.RELOCATION
    database = deploy_database.Database(
        config,
        state,
        reporter=reporter,
        runner=runner,
        lock=lock,
        role="relocation target" if relocating else "database",
    )
    relocation = (
        _relocation(config, state, reporter, runner, database, lock)
        if relocating
        else None
    )
    recovery = deploy_recovery.Recovery(
        config,
        state,
        reporter=reporter,
        runner=runner,
        lock=lock,
        diagnostics=diagnostics,
        versions=versions,
        database=database,
        relocation=relocation,
    )
    pipeline = Pipeline(
        config,
        reporter=reporter,
        versions=versions,
        database=database,
        recovery=recovery,
        relocation=relocation,
    )
    return pipeline, diagnostics


def _relocation(
    config: deploy_config.DeployConfig,
    state: deploy_state.PipelineState,
    reporter: deploy_reporting.Reporter,
    runner: wrangler_runner.WranglerRunner,
    target: deploy_database.Database,
    target_lock: deploy_lock.LockReader,
) -> deploy_relocation.Relocation:
    """The source database's collaborators, built like the target's.

    The source gets the same lock, read-back and unlock machinery the windowed
    body uses, pointed at its name: wrangler resolves a database name against
    the account when the checkout's config no longer holds it, which is the
    case here, since the checkout binds the target.
    """
    source_config = dataclasses.replace(config, db_name=config.relocation_source_db)
    source_state = deploy_state.PipelineState()
    source_lock = deploy_lock.LockReader(
        wrangler=config.wrangler,
        db_name=config.relocation_source_db,
        wrangler_env=config.wrangler_env,
        verify_backoff=config.verify_backoff,
    )
    source = deploy_database.Database(
        source_config,
        source_state,
        reporter=reporter,
        runner=runner,
        lock=source_lock,
        role="relocation source",
    )
    return deploy_relocation.Relocation(
        config,
        state,
        reporter=reporter,
        source=source,
        source_state=source_state,
        source_lock=source_lock,
        target=target,
        target_lock=target_lock,
    )


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
