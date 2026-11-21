"""What the Worker is serving: reading it, uploading to it, and moving it.

The pipeline captures one serving version as the rollback target. The captured
ID must still serve all traffic before migration and promotion. A gradual split is not a legal starting state: this pipeline
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

SERVING_REFUSAL = (
    "[FAIL] Verify the deployment history before retrying. Only provision a Worker "
    "separately after confirming it has never been deployed. For a confirmed "
    "traffic split, complete or roll back the gradual "
    "rollout so one version serves 100%."
)


def utc_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def view_version(
    runner: wrangler_runner.WranglerRunner,
    wrangler_env: str,
    version_id: str,
    state: deploy_state.PipelineState,
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
        "versions_view",
        "view",
        version_id,
        ["--json", "--env", wrangler_env],
        retry_lookup_for=state.uploaded_version,
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

    def capture_single_serving_version(self) -> str:
        """The windowless capture: the serving set must be exactly one version.

        Recorded as the rollback target for a failed final promote and the
        reference checked before migration and promotion. Provision a new
        Worker separately so an unreadable deployment cannot be mistaken for one.
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
        return state.serving_version_id

    def verify_single_serving_version(self, context: str) -> None:
        """TOCTOU re-check: the serving set must still be the recorded version.

        A mismatch before migration leaves the database untouched. At final
        promotion or a visibility retry the database has already migrated;
        aborting preserves the serving version or split that intervened.
        A deployment between the check and the action is the operational
        exclusion: another control plane moved traffic. The next preflight
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

    def upload_final_version(self) -> None:
        config, state = self._config, self._state
        self._reporter.step(f"Upload final version ({config.env})")
        state.new_final_id = self._upload_version(version_message.FINAL, [])
        emit(f"New final version: {state.new_final_id}")
        self._reporter.summary(f"| Final version | `{state.new_final_id}` |")

    def verify_final_runtime(self) -> None:
        """Check the immutable upload before it can receive traffic."""
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
        document = view_version(
            self._runner, config.wrangler_env, version_id, self._state
        )
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
        """Sync configured triggers before migration or code promotion.

        Wrangler can apply some trigger changes before another fails. Recovery
        must inspect the live settings even when the version did not change.
        """
        config = self._config
        self._reporter.step(f"Update triggers ({config.env})")
        result = self._runner.run(
            "triggers_deploy", ["triggers", "deploy", "--env", config.wrangler_env]
        )
        self._require(result)

    def promote_final_version(self) -> None:
        config, state = self._config, self._state

        def prepare_promotion() -> None:
            self.verify_single_serving_version("before the final promotion")
            self._reporter.step(f"Promote final version to 100% ({config.env})")

        prepare_promotion()

        # A failed submission can have applied server-side. The runner marks
        # ambiguous attempts and clears a rejected lookup before its retry.
        def mark_attempt(in_flight: bool) -> None:
            state.final_promote_attempted = in_flight

        self._promote(
            "promote_final",
            state.new_final_id,
            version_message.FINAL,
            mark_attempt,
            before_visibility_retry=prepare_promotion,
        )
        # Cleared on success so a failure in a later step - the windowless
        # postflight, the closing timing - does not roll back a promote that
        # already landed.
        state.final_promote_attempted = False

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

        Capture validates the rollback target before passing it to Wrangler.
        Later rechecks only compare against this captured version.
        """
        try:
            version_id = deployments_list.single_serving_version_id(document)
        except deployments_list.Refused as refusal:
            emit_error(f"[FAIL] single-version check failed: {refusal}")
            emit_error(SERVING_REFUSAL)
            raise DeployAbort(1) from refusal
        # Recovery must be able to pass the captured ID back to Wrangler.
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
        recorded, before the first mutation. Rechecks hold the captured ID up
        to migration and promotion.
        """
        expected = self._config.expected_serving_sha
        document = view_version(
            self._runner, self._config.wrangler_env, version_id, self._state
        )
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
            retry_lookup_for=self._state.uploaded_version,
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
