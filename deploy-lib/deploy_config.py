"""What one deploy run was asked to do, resolved once before anything runs.

The wrapper supplies the Wrangler environment, commit, runtime expectations
and hook commands. Config errors must stop the run before any live mutation.
Hooks receive the target environment. Preflight also receives the serving version ID.

A command string is split with `shlex`, not on whitespace, so a hook path
containing a space is expressible when it is quoted.
"""

from __future__ import annotations

import dataclasses
import re
import shlex
import subprocess
from collections.abc import Mapping, Sequence

import version_runtime

# A previous pin must reject an upgraded wrapper before running its old hooks.
WINDOWLESS = "windowless-v1"

ENVIRONMENTS = ("staging", "production")

FULL_SHA = re.compile(r"[0-9a-f]{40}")

# What a consumer that sets none of them gets. Each is overridable by the
# matching environment variable; `deploy-lib/README.md` documents them.
DEFAULT_WRANGLER = "npx wrangler"
USAGE = """Usage: %s <staging|production> [--sha SHA] [--expected-serving-sha SHA]

  --sha                   7-char commit hash for deploy metadata (default: git rev-parse)
  --expected-serving-sha  full commit the version serving 100%% must be a final upload of
"""


class UsageError(Exception):
    """argv did not describe a deploy. Exits 2, distinct from a deploy failure."""


class ConfigError(Exception):
    """The environment does not describe a deploy this pipeline can run."""


def _whole_seconds(raw: str, field: str) -> int:
    """Reject retry intervals that sleep cannot honor."""
    try:
        seconds = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{field} must be whole seconds, got {raw!r}") from exc
    if seconds < 0:
        raise ConfigError(f"{field} must not be negative, got {raw!r}")
    return seconds


def _backoff(raw: str, field: str) -> tuple[int, ...]:
    """A space-separated schedule as whole seconds.

    Refused rather than skipped when an item is not a duration: a schedule the
    operator wrote and the pipeline silently dropped is a retry budget nobody
    has.
    """
    return tuple(_whole_seconds(item, f"{field} item") for item in raw.split())


def _hook(raw: str | None) -> tuple[str, ...] | None:
    """A hook command as argv, or None when the consumer set no command."""
    if raw is None or not raw.strip():
        return None
    return tuple(shlex.split(raw))


def _git_short_sha() -> str:
    """The `--sha` default: the working tree's HEAD, or unknown."""
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "--short=7", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        # No git on PATH. The sha is deploy metadata rather than a decision, so
        # the run continues with a placeholder rather than refusing.
        return "unknown"
    if completed.returncode != 0:
        return "unknown"
    return completed.stdout.strip() or "unknown"


@dataclasses.dataclass(frozen=True)
class Invocation:
    """The environment, SHA and expected serving commit argv carries.

    The expected serving commit is an argument, not an environment variable,
    because a caller cannot know which pipeline its target checkout carries: a
    pipeline that predates the bind refuses an argument it does not know, where
    it would ignore a variable and deploy unbound.
    """

    env: str
    sha: str
    expected_serving_sha: str = ""


def parse_args(argv: Sequence[str]) -> Invocation:
    """`<staging|production> [--sha SHA] [--expected-serving-sha SHA]`.

    `--sha` with nothing after it leaves the sha unset and takes the git
    default, rather than refusing: a trailing flag is a wrapper passing through
    an unset variable, and the default is the value it was reaching for. A
    `--sha` followed by another flag or an environment name is refused instead:
    reading that word as the sha would drop it from the command line, and the
    command would mean something its caller never wrote.
    """
    env = ""
    sha = ""
    expected_serving_sha = ""
    index = 0
    while index < len(argv):
        argument = argv[index]
        if argument in ENVIRONMENTS:
            env = argument
        elif argument == "--sha":
            index += 1
            sha = argv[index] if index < len(argv) else ""
            if sha.startswith("-") or sha in ENVIRONMENTS:
                raise UsageError(f"--sha takes a commit, not {sha!r}")
        elif argument == "--expected-serving-sha":
            # Unlike --sha, an empty value has no default to fall back on: read
            # as "no expected build", a trailing or blanked flag would turn the
            # bind off, and a repeat would replace the value the caller checked.
            index += 1
            if index >= len(argv) or not argv[index] or expected_serving_sha:
                raise UsageError("--expected-serving-sha takes one commit, once")
            expected_serving_sha = argv[index]
        elif argument in ("-h", "--help"):
            raise UsageError("")
        else:
            raise UsageError(f"Unknown argument: {argument}")
        index += 1

    if not env:
        raise UsageError("Error: environment (staging|production) is required.")
    return Invocation(
        env=env,
        sha=sha or _git_short_sha(),
        expected_serving_sha=expected_serving_sha,
    )


def usage_text(program: str) -> str:
    """The usage block, printed on stderr before a usage exit."""
    return USAGE % program


@dataclasses.dataclass(frozen=True)
class DeployConfig:
    """One run's settled configuration. Nothing here changes once the run starts."""

    env: str
    sha: str
    wrangler_env: str
    wrangler: tuple[str, ...]
    version_message_annotation: str
    # The full commit being deployed, or empty. Set, every upload's message
    # names it (version_message.py), which is what a later expected-build bind
    # matches on; `sha` is only its displayed short form.
    commit_sha: str
    # The full commit the caller expects to be serving, or empty. Set, the
    # capture refuses unless the single serving version was uploaded from it
    # (deploy_versions.WorkerVersions._require_expected_build).
    expected_serving_sha: str
    # The runtime every uploaded version must be recorded with, or None. Set,
    # the pipeline reads each body's checked upload back and refuses one at any
    # other (deploy_versions.WorkerVersions._verify_uploaded_runtime); None
    # makes no read at all.
    expected_runtime: version_runtime.Runtime | None
    diagnostics_dir: str | None
    retry_backoff: tuple[int, ...] | None
    preflight_cmd: tuple[str, ...]
    migrate_cmd: tuple[str, ...]
    postflight_cmd: tuple[str, ...] | None
    github_actions: bool
    github_step_summary: str | None


def resolve(invocation: Invocation, environ: Mapping[str, str]) -> DeployConfig:
    """Refuse incompatible configurations before any live mutation."""
    mode = environ.get("DEPLOY_PIPELINE_MODE", "")
    if mode != WINDOWLESS:
        raise ConfigError(
            f"deploy-lib: unrecognized DEPLOY_PIPELINE_MODE '{mode}' (expected '{WINDOWLESS}')"
        )

    key = f"DEPLOY_WRANGLER_ENV_{invocation.env.upper()}"
    wrangler_env = environ.get(key)
    if not wrangler_env:
        raise ConfigError(f"deploy-lib: set {key} for env={invocation.env}")

    # Older wrappers delegated the database fence check to the toolkit. A
    # distinct hook name makes upgrading a deliberate transfer of that check.
    preflight_cmd = _hook(environ.get("WINDOWLESS_PREFLIGHT_CMD"))
    if preflight_cmd is None:
        raise ConfigError(
            "deploy-lib: set WINDOWLESS_PREFLIGHT_CMD to verify live migration safety before deployment"
        )

    migrate_cmd = _hook(environ.get("MIGRATE_CMD"))
    if migrate_cmd is None:
        raise ConfigError(
            "deploy-lib: MIGRATE_CMD is not set. The pipeline runs the migrate "
            "hook as a command with the environment name as its last argument; "
            "set MIGRATE_CMD to that command (use `true` for a deploy that "
            "applies no migration)."
        )

    commit_sha = environ.get("DEPLOY_COMMIT_SHA", "")
    if commit_sha and not (
        FULL_SHA.fullmatch(commit_sha) and commit_sha.startswith(invocation.sha)
    ):
        # A message naming one commit beside another's short form would leave
        # the version's own record contradicting itself.
        raise ConfigError(
            "deploy-lib: DEPLOY_COMMIT_SHA must be the full lowercase commit "
            f"that --sha '{invocation.sha}' abbreviates, got '{commit_sha}'"
        )
    expected_serving_sha = invocation.expected_serving_sha
    if expected_serving_sha and not FULL_SHA.fullmatch(expected_serving_sha):
        raise ConfigError(
            "deploy-lib: --expected-serving-sha must be a full lowercase "
            f"commit SHA, got '{expected_serving_sha}'"
        )

    # Unset is "no check"; set and empty is a consumer whose own read of its
    # configured runtime produced nothing, which must not pass for unset.
    expected_runtime = None
    expected_runtime_text = environ.get("DEPLOY_EXPECTED_RUNTIME")
    if expected_runtime_text is not None:
        try:
            expected_runtime = version_runtime.expected(expected_runtime_text)
        except version_runtime.Unreadable as unreadable:
            raise ConfigError(f"deploy-lib: {unreadable}") from unreadable

    diagnostics_dir = environ.get("DEPLOY_DIAGNOSTICS_DIR") or None

    return DeployConfig(
        env=invocation.env,
        sha=invocation.sha,
        wrangler_env=wrangler_env,
        wrangler=tuple(shlex.split(environ.get("WRANGLER") or DEFAULT_WRANGLER)),
        version_message_annotation=environ.get("DEPLOY_VERSION_MESSAGE_ANNOTATION", ""),
        commit_sha=commit_sha,
        expected_serving_sha=expected_serving_sha,
        expected_runtime=expected_runtime,
        diagnostics_dir=diagnostics_dir,
        retry_backoff=(
            _backoff(schedule, "DEPLOY_WRANGLER_RETRY_BACKOFF_SECONDS")
            if (schedule := environ.get("DEPLOY_WRANGLER_RETRY_BACKOFF_SECONDS"))
            else None
        ),
        preflight_cmd=preflight_cmd,
        migrate_cmd=migrate_cmd,
        postflight_cmd=_hook(environ.get("POSTFLIGHT_CMD")),
        github_actions=bool(environ.get("GITHUB_ACTIONS")),
        github_step_summary=environ.get("GITHUB_STEP_SUMMARY") or None,
    )
