"""What the Worker is serving: reading it, uploading to it, and moving it.

Every step here writes one of the version fields on the run's state - the
rollback target, the recorded serving version, the maintenance and final uploaded
version ids, and the flags that say whether a promoted version may be live. Nothing else in
the pipeline writes them, and the recovery path's Worker rollback is the last
member of the same set, which is why it lives here rather than beside the
recovery layers that call it.

The serving set has to be exactly one version at 100%, and the windowed and
windowless captures refuse
anything else. A gradual split is not a legal starting state: this pipeline
records ONE rollback target and, on failure, sends all traffic back to it, so
under a split that target would be whichever slice Cloudflare happened to list
first - a version the run never reasoned about. Which shapes refuse and why is
deployments_list.py.
"""

from __future__ import annotations

import datetime
import json
import re
from collections.abc import Callable, Sequence

import deploy_config
import deploy_reporting
import deploy_state
import deployments_list
import version_message
import version_runtime
import wrangler_runner
from deploy_reporting import emit, emit_error
from deploy_state import DeployAbort

# wrangler's documented line on a successful `versions upload`. The id is what
# the promote pins its traffic split to, so a miss here aborts rather than
# promoting something unexamined.
VERSION_ID_LINE = re.compile(r"Worker Version ID: ([0-9a-f-]+)")

SPLIT_SERVING_REFUSAL = (
    "[FAIL] The serving set is not a single version at 100%. Resolve the split "
    "deliberately (complete or roll back the gradual rollout so one version "
    "serves 100%), then re-dispatch."
)


def utc_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def view_version(
    runner: wrangler_runner.WranglerRunner,
    wrangler_env: str,
    version_id: str,
) -> object | None:
    """One version's document, or None when it could not be read.

    The document is what `wrangler versions view --json` prints, the `result`
    of Cloudflare's version endpoint: its annotations (the upload message
    version_message.py reads, naming the commit) and the metadata and resources it was uploaded
    with. The read goes through the run's retry runner. Whichever way it fails,
    the reason is in the log when this returns None: wrangler's stderr for a
    failed call, and this function's own line for a call that succeeded without
    printing a document, which can leave nothing on stderr at all.
    """
    result = runner.run_version(
        "versions_view", "view", version_id, ["--json", "--env", wrangler_env]
    )
    if not result.ok:
        return None
    try:
        document: object = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        emit_error(
            f"[FAIL] versions view {version_id} printed no JSON document "
            f"({error}): {result.stdout[:200]!r}"
        )
        return None
    return document


class WorkerVersions:
    """The Worker-version half of a deploy: capture, upload, promote, roll back."""

    def __init__(
        self,
        config: deploy_config.DeployConfig,
        state: deploy_state.PipelineState,
        *,
        reporter: deploy_reporting.Reporter,
        runner: wrangler_runner.WranglerRunner,
    ) -> None:
        self._config = config
        self._state = state
        self._reporter = reporter
        self._runner = runner

    # --- capture -----------------------------------------------------------

    def capture_previous_version_id(self) -> None:
        """Record the rollback target the recovery path will use.

        A list that cannot be read refuses the deploy in either environment: a
        transient wrangler auth or network failure read as a first-ever deploy
        would disable the Worker rollback for the rest of the run. A Worker's
        first deploy, which has no list to read, goes out once as a plain
        `wrangler deploy` (the README's Limits).
        """
        config, state = self._config, self._state
        self._reporter.step(f"Capture previous version ID ({config.env})")
        result = self._deployments_list()
        if not result.ok:
            emit_error(
                "[FAIL] Could not list deployments (see stderr above for "
                "wrangler output)"
            )
            emit_error(
                "[FAIL] Refusing to treat an unreadable deployments list as a "
                "first deploy. A Worker's first deploy goes out once as a plain "
                "`wrangler deploy`."
            )
            raise DeployAbort(1)

        # The bootstrap shape is checked first and separately: a readable list
        # with no deployment - or no version under the newest one - is the one
        # emptiness this body legitimately continues through, and staging goes
        # on with no rollback target. "Nothing has deployed yet" must never be
        # reachable by an extraction failure, or staging proceeds with no
        # rollback target on evidence nobody checked.
        if deployments_list.is_bootstrap_shape(result.stdout):
            if config.expected_serving_sha:
                emit_error(
                    "[FAIL] Nothing is serving, so the expected build "
                    f"{config.expected_serving_sha} is not. Refusing to deploy."
                )
                raise DeployAbort(1)
            state.prev_version_id = ""
        else:
            state.prev_version_id = self._single_serving_version(result.stdout)

        if not state.prev_version_id and config.is_production:
            emit_error(
                "[FAIL] Could not extract previous version ID from deployments "
                "list - refusing to deploy production without a rollback target"
            )
            raise DeployAbort(1)
        emit(f"Previous version: {state.prev_version_id or '<none - first deploy>'}")
        self._reporter.summary(
            f"| Previous version | `{state.prev_version_id or 'none'}` |"
        )

    def capture_single_serving_version(self) -> None:
        """The windowless capture: the serving set must be exactly one version.

        Recorded as the rollback target for a failed final promote and the
        reference the TOCTOU re-checks match. Unlike the windowed capture
        there is no bootstrap tolerance: with no serving version there is
        nothing for the single-version invariant to describe, so a first-ever
        deploy has to run the windowed body. Nothing falls back silently.
        """
        config, state = self._config, self._state
        self._reporter.step(f"Single-version preflight ({config.env})")
        result = self._deployments_list()
        if not result.ok:
            emit_error(
                "[FAIL] Could not list deployments - the windowless path refuses "
                "to run without a verified single serving version (see stderr above)"
            )
            raise DeployAbort(1)

        state.serving_version_id = self._single_serving_version(result.stdout)
        state.prev_version_id = state.serving_version_id
        emit(f"Serving version (single, 100%): {state.serving_version_id}")
        self._reporter.summary(f"| Serving version | `{state.serving_version_id}` |")

    def verify_single_serving_version(self, context: str) -> None:
        """TOCTOU re-check: the serving set must still be the recorded version.

        Runs immediately before the first DB mutation and immediately before the
        final promotion. Before the mutation an abort leaves everything
        untouched; before the promotion it leaves the DB migrated and the old
        version serving - the fix-forward state the consumer's preflight
        admitted. A deployment landing in the seconds between a re-check and the
        action it guards is the documented operational exclusion: another
        control plane moved it, and the next sanctioned deploy's preflight
        detects the result.
        """
        config, state = self._config, self._state
        self._reporter.step(
            f"Re-verify single serving version {context} ({config.env})"
        )
        result = self._deployments_list()
        if not result.ok:
            emit_error(
                f"[FAIL] Could not re-read the serving set {context} - aborting "
                "rather than proceeding on an unverified serving state"
            )
            raise DeployAbort(1)

        try:
            observed = deployments_list.single_serving_version_id(result.stdout)
        except deployments_list.Refused as refusal:
            emit_error(f"[FAIL] single-version check failed: {refusal}")
            emit_error(
                f"[FAIL] Serving set changed mid-deploy ({context}): no longer a "
                "single version at 100%. Aborting - find what moved it before "
                "re-dispatching."
            )
            raise DeployAbort(1) from refusal

        if observed != state.serving_version_id:
            emit_error(
                f"[FAIL] Serving set changed mid-deploy ({context}): expected "
                f"{state.serving_version_id}, found {observed} serving. Aborting "
                "- find what promoted mid-deploy before re-dispatching."
            )
            raise DeployAbort(1)
        emit(f"Serving version unchanged: {state.serving_version_id}")

    # --- upload and promote ------------------------------------------------

    def upload_maintenance_version(self) -> None:
        config, state = self._config, self._state
        self._reporter.step(f"Upload maintenance-mode version ({config.env})")
        state.new_maint_id = self._upload_version(
            version_message.MAINTENANCE, ["--var", "MAINTENANCE_MODE:true"]
        )
        emit(f"New maintenance version: {state.new_maint_id}")
        self._reporter.summary(f"| Maintenance version | `{state.new_maint_id}` |")

    def upload_final_version(self) -> None:
        config, state = self._config, self._state
        self._reporter.step(f"Upload final version ({config.env})")
        state.new_final_id = self._upload_version(version_message.FINAL, [])
        emit(f"New final version: {state.new_final_id}")
        self._reporter.summary(f"| Final version | `{state.new_final_id}` |")

    def verify_maintenance_runtime(self) -> None:
        """The windowed body's runtime check, on its first upload.

        The maintenance version is read back, before the triggers update and its
        promote, so a failed or slow read holds nothing paused. The final
        version is not read back: it is uploaded inside the window, where a
        read failure would cost the full recovery, and it comes from the same
        checkout, wrangler environment and process as the maintenance version,
        differing from it only by a `--var`, which sets no runtime.
        """
        self._verify_uploaded_runtime(self._state.new_maint_id)

    def verify_final_runtime(self) -> None:
        """The windowless and relocation bodies' runtime check, on their upload.

        Each uploads only its final version. The relocation body uploads before
        it fences the source, so this read is outside the write pause too.
        """
        self._verify_uploaded_runtime(self._state.new_final_id)

    def _verify_uploaded_runtime(self, version_id: str) -> None:
        """Refuse an uploaded version Cloudflare would run at another runtime.

        The expected runtime is the consumer's configured one - for a consumer
        whose tests run at it, the runtime they ran at - so a version recorded
        with any other would serve code nothing checked on that runtime. Read from the version before it is promoted, while
        refusing it costs only the orphan upload. Without an expected runtime
        this makes no call and writes nothing, so a consumer that does not set
        one deploys exactly as before.

        Each way it stops says which it is, because only a mismatch is a
        statement about the runtime; a version document that states no runtime,
        and a read that failed, are not.
        """
        config = self._config
        expected = config.expected_runtime
        if expected is None:
            return
        self._reporter.step(f"Verify the uploaded version's runtime ({config.env})")
        document = view_version(self._runner, config.wrangler_env, version_id)
        if document is None:
            emit_error(
                f"[FAIL] RUNTIME NOT CHECKED: reading uploaded version {version_id} "
                "back from Cloudflare failed (wrangler's error and any retries are "
                "above). This is a failed read, not a runtime mismatch. Nothing was "
                "promoted by this step; re-dispatch once Cloudflare's API answers."
            )
            raise DeployAbort(1)
        try:
            actual = version_runtime.of_version(document)
        except version_runtime.Unreadable as unreadable:
            emit_error(
                f"[FAIL] RUNTIME NOT CHECKED: uploaded version {version_id}'s "
                f"document does not state a runtime this pipeline can read "
                f"({unreadable}). This is not a runtime mismatch. Nothing was "
                "promoted by this step; a re-dispatch reads the same shape, so "
                "find what changed in `wrangler versions view --json` first."
            )
            raise DeployAbort(1) from unreadable
        if actual != expected:
            emit_error(
                f"[FAIL] RUNTIME MISMATCH: uploaded version {version_id} would run "
                f"at {actual.describe()}, but the expected runtime for "
                f"{config.wrangler_env} is {expected.describe()}. Nothing was "
                "promoted by this step. DEPLOY_EXPECTED_RUNTIME is the consumer's "
                "configured runtime, so find why the upload differs before "
                "re-dispatching."
            )
            raise DeployAbort(1)
        emit(
            f"Uploaded version {version_id} runs at {actual.describe()}; the "
            f"expected runtime for {config.wrangler_env} is {expected.describe()}"
        )

    def update_triggers(self) -> None:
        """Push the cron schedule and routes from the wrangler config.

        CRITICAL: this runs BEFORE the maintenance promote, so a cron-limit
        failure - the incident this pipeline exists for - aborts with nothing
        live.
        """
        config = self._config
        self._reporter.step(f"Update triggers ({config.env})")
        result = self._runner.run(
            "triggers_deploy", ["triggers", "deploy", "--env", config.wrangler_env]
        )
        self._require(result)

    def promote_maintenance_version(self) -> None:
        config, state = self._config, self._state
        self._reporter.step(f"Promote maintenance version to 100% ({config.env})")

        # Marked in flight BEFORE the call, the same way the final promote is
        # and for the same reason (see the flag's declaration in
        # deploy_state.py). Rollback to a pinned version is idempotent, so a
        # promote that did NOT apply just re-pins the version already serving.
        def mark_attempt(in_flight: bool) -> None:
            state.maint_promote_attempted = in_flight

        # Idempotent: the version id pins the target, so repeating the call
        # converges on the same traffic split.
        self._promote(
            "promote_maint",
            state.new_maint_id,
            version_message.MAINTENANCE,
            mark_attempt,
        )
        state.maint_live = True
        # Cleared on success: from here maint_live carries the rollback
        # obligation, and a stale flag would roll back a deploy that completed.
        state.maint_promote_attempted = False

    def promote_final_version(self, guard: Callable[[], None] | None = None) -> None:
        config, state = self._config, self._state
        self._reporter.step(f"Promote final version to 100% ({config.env})")

        # Marked in flight BEFORE the call, as for the maintenance promote:
        # `versions deploy @100%` can apply server-side and still return
        # non-zero after retries. On the windowless body maint_live is never
        # true, so without this flag the recovery path would skip the rollback
        # and leave the new, maybe-live version up while reporting "old version
        # still serving". Rollback to a pinned version is idempotent, so a
        # promote that did NOT apply just no-ops.
        def recheck() -> None:
            if guard is not None:
                guard()
            self._reporter.step(f"Promote final version to 100% ({config.env})")

        def mark_attempt(in_flight: bool) -> None:
            state.final_promote_attempted = in_flight

        self._promote(
            "promote_final",
            state.new_final_id,
            version_message.FINAL,
            mark_attempt,
            before_visibility_retry=recheck if guard is not None else None,
        )
        state.maint_live = False
        # Cleared on success so a failure in a later step - the windowless
        # postflight, the closing timing - does not roll back a promote that
        # already landed.
        state.final_promote_attempted = False

    def verify_final_version_is_serving(self) -> None:
        """The promoted version, alone, at 100% - read back, not inferred.

        The relocation body's gate on opening its target. The promote's exit
        status says the call returned; this says what serves. Opening the
        target under a version that does not read it would record the
        relocation as complete while every write is still refused, so anything
        but the final version serving alone aborts with source and target locked.
        """
        config, state = self._config, self._state
        self._reporter.step(f"Verify the final version is what serves ({config.env})")
        result = self._deployments_list()
        if not result.ok:
            emit_error(
                "[FAIL] Could not read the serving set after the promotion - "
                "refusing to open the target on an unverified serving state"
            )
            raise DeployAbort(1)
        try:
            observed = deployments_list.single_serving_version_id(result.stdout)
        except deployments_list.Refused as refusal:
            emit_error(f"[FAIL] single-version check failed: {refusal}")
            raise DeployAbort(1) from refusal
        if observed != state.new_final_id:
            emit_error(
                f"[FAIL] Promoted {state.new_final_id} and found {observed} "
                "serving. Refusing to open the target."
            )
            raise DeployAbort(1)
        emit(f"Serving version (single, 100%): {observed}")

    # --- recovery ----------------------------------------------------------

    def roll_back_worker(self) -> bool:
        """Send all traffic back to the recorded previous version.

        Called only by the recovery path, and only when a promoted version may
        be the one currently serving. A transient API failure here is the worst
        possible moment for a one-shot miss, and `rollback` is idempotent at a
        specific version id, so the retry wrapping is pure upside.
        """
        config, state = self._config, self._state
        result = self._runner.run(
            "wrangler_rollback",
            [
                "rollback",
                state.prev_version_id,
                "--env",
                config.wrangler_env,
                "--yes",
                "--message",
                f"deploy-pipeline recovery from failed deploy ({config.sha})",
            ],
        )
        wrangler_runner.write_stdout(result.stdout)
        return result.ok

    # --- helpers -----------------------------------------------------------

    def _deployments_list(self) -> wrangler_runner.WranglerResult:
        """The `deployments list --json` read the captures and the re-checks use.

        Its stdout is a document the caller parses, so it is deliberately not
        echoed: the runner has already put wrangler's stderr in the log, and
        merging the two would break the parse on a retry-then-success run.
        """
        return self._runner.run(
            "deployments_list",
            ["deployments", "list", "--json", "--env", self._config.wrangler_env],
        )

    def _single_serving_version(self, document: str) -> str:
        """The serving version id, fit to record as the rollback target.

        The windowed and windowless captures come through here. The windowless
        capture records the same id as the rollback target and TOCTOU reference,
        so the usability check
        belongs here rather than at either call site. The TOCTOU re-check reads
        the extractor directly and stays outside it on purpose: it compares an
        observed id against the recorded one and never hands it to wrangler, and
        an id that fails this check would differ from the recorded one anyway,
        which is already an abort. The expected-build bind is here for the
        usability check's reason: every capture comes through it.
        """
        try:
            version_id = deployments_list.single_serving_version_id(document)
        except deployments_list.Refused as refusal:
            emit_error(f"[FAIL] single-version check failed: {refusal}")
            emit_error(SPLIT_SERVING_REFUSAL)
            raise DeployAbort(1) from refusal
        # An id the recovery path cannot spend is not a rollback target, so it
        # is refused here rather than where it would be spent. Refused at the
        # capture, the deploy stops with nothing uploaded, promoted or locked.
        # Left to run, it reaches `wrangler rollback <id>` during recovery,
        # where the rollback now fails cleanly rather than escaping - but a
        # rollback that cannot run is still one that leaves the serving version
        # unknown, which withholds the unlock and hands the operator a locked
        # database to reconcile by hand. Refusing before the window opens is the
        # cheaper end of the same invariant.
        if not wrangler_runner.reaches_wrangler(version_id):
            emit_error(
                "[FAIL] The serving version id cannot be passed back to wrangler,"
                " so it is not a usable rollback target. Refusing to deploy."
            )
            raise DeployAbort(1)
        if self._config.expected_serving_sha:
            self._require_expected_build(version_id)
        return version_id

    def _require_expected_build(self, version_id: str) -> None:
        """Refuse unless the serving version was uploaded from the expected build.

        The caller checked what this deploy changes against that build. A
        version serving from any other - one a dashboard rollback brought back
        since, or one uploaded outside this pipeline - would have the run
        deploy over a state nobody checked. It is read where the id is
        recorded, before the first mutation. On the windowless and relocation
        bodies the TOCTOU re-checks then hold that id up to the first DB
        mutation and the promote; the windowed body has no re-check, so there
        the bind holds at the capture only.
        """
        expected = self._config.expected_serving_sha
        document = view_version(self._runner, self._config.wrangler_env, version_id)
        if document is None:
            emit_error(
                f"[FAIL] Could not read serving version {version_id} (see above), "
                "so its build is unknown. Refusing to deploy."
            )
            raise DeployAbort(1)
        commit = version_message.final_commit(document)
        if commit != expected:
            emit_error(
                f"[FAIL] Serving version {version_id} was uploaded from "
                f"{commit or 'no commit its message names'}, not the expected "
                f"{expected}. Refusing to deploy over a serving state the caller "
                "did not check."
            )
            raise DeployAbort(1)
        emit(f"Serving version {version_id} is the expected build {expected}")

    def _upload_version(self, label: str, extra_args: Sequence[str]) -> str:
        """Upload a version and return its id.

        The id is parsed out of the TERMINAL attempt's output. The retry runner
        keeps the attempts apart, so the parse cannot pick up an id from an
        earlier attempt or a retry diagnostic.

        `versions upload` is the one wrapped operation that is not idempotent:
        each successful call mints a fresh version id, so a retry after a lost
        response leaves an orphan version behind. That is harmless - orphan
        versions are cheap, and the succeeding attempt's id is the one promoted.
        """
        config = self._config
        timestamp = utc_now().strftime("%Y-%m-%dT%H:%M:%SZ")
        # An optional consumer token rides at the end of the message, which
        # makes it part of the version's immutable annotations - readable later
        # through `versions view` and the dashboard's version list.
        message = version_message.upload_message(
            label, config.sha, config.commit_sha, config.version_message_annotation
        )

        result = self._runner.run(
            f"upload_{label}",
            [
                "versions",
                "upload",
                "--env",
                config.wrangler_env,
                "--define",
                f"__DEPLOY_TIMESTAMP__:'{timestamp}'",
                "--define",
                f"__DEPLOY_SHA__:'{config.sha}'",
                "--message",
                message,
                *extra_args,
            ],
        )
        # On stderr, not stdout: an operator still sees wrangler's upload output
        # in the CI log, and this call's own return value stays the version id.
        wrangler_runner.write_stderr(result.stdout)
        if not result.ok:
            raise DeployAbort(result.returncode)

        found = VERSION_ID_LINE.search(result.combined)
        if found is None:
            emit_error(
                f"[FAIL] Could not extract version ID from upload output "
                f"(label={label})"
            )
            raise DeployAbort(1)
        return found.group(1)

    def _promote(
        self,
        label: str,
        version_id: str,
        kind: str,
        mark_attempt: Callable[[bool], None],
        *,
        before_visibility_retry: Callable[[], None] | None = None,
    ) -> None:
        result = self._runner.run_version(
            label,
            "deploy",
            version_id,
            [
                "--env",
                self._config.wrangler_env,
                "--yes",
                "--message",
                f"deploy-pipeline promote {kind} ({self._config.sha})",
            ],
            before_visibility_retry=before_visibility_retry,
            mark_attempt=mark_attempt,
        )
        self._require(result)

    @staticmethod
    def _require(result: wrangler_runner.WranglerResult) -> None:
        wrangler_runner.write_stdout(result.stdout)
        if not result.ok:
            raise DeployAbort(result.returncode)

    def _wrangler_text(self) -> str:
        return " ".join(self._config.wrangler)
