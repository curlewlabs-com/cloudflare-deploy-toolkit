"""What one deploy run was asked to do, resolved once before anything runs.

Everything the pipeline needs to know that is not a decision about live state:
the environment and its D1 database, the commit being deployed, which body
runs, the hook commands, the retry schedules, and where diagnostics go. All of
it comes from argv and the environment, and all of it is settled here so that a
misconfigured run fails before the first wrangler call rather than part-way
through a maintenance window.

THE HOOK CONTRACT IS COMMANDS. The pipeline is a program a consumer's wrapper
starts rather than a library it sources, so the hooks are commands the
pipeline runs as subprocesses with the environment name appended as their last
argument, named by `PREFLIGHT_CMD`, `MIGRATE_CMD` and `POSTFLIGHT_CMD`, plus
`RELOCATE_COPY_CMD` on the relocation body.

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

WINDOWED = "windowed"
WINDOWLESS = "windowless"
RELOCATION = "relocation"
MODES = (WINDOWED, WINDOWLESS, RELOCATION)

ENVIRONMENTS = ("staging", "production")

FULL_SHA = re.compile(r"[0-9a-f]{40}")

# What a consumer that sets none of them gets. Each is overridable by the
# matching environment variable; `deploy-lib/README.md` documents them.
DEFAULT_WRANGLER = "npx wrangler"
DEFAULT_DRAIN_SECONDS = 15
DEFAULT_RETRY_BACKOFF = "2 5 15"
# How long a relocation may hold its source fenced before it must have reached
# the promote. It bounds the time to the promote, not the pause, so a consumer's
# ceiling on the whole pause has to leave room for whichever way out is longer:
# the promote, the serving check and opening the target; or, when a hook is
# stopped at the deadline, the stop sequence's grace period and then reopening
# the source.
DEFAULT_RELOCATION_MAX_PAUSE_SECONDS = 240
DEFAULT_VERIFY_BACKOFF = "1 2 4 8"

USAGE = """Usage: %s <staging|production> [--sha SHA] [--expected-serving-sha SHA] [--dry-run]

  --sha                   7-char commit hash for deploy metadata (default: git rev-parse)
  --expected-serving-sha  full commit the version serving 100%% must be a final upload of
  --dry-run               Print commands without executing
"""


class UsageError(Exception):
    """argv did not describe a deploy. Exits 2, distinct from a deploy failure."""


class ConfigError(Exception):
    """The environment does not describe a deploy this pipeline can run."""


def _whole_seconds(raw: str, field: str) -> int:
    """A duration the pipeline can actually wait, or a refusal.

    Negative and non-numeric values are refused because `sleep`
    cannot honor. Refusing here rather than at the wait matters because the
    windowed body does not reach its first wait until the maintenance version is
    already serving.
    """
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
    """The environment, SHA, expected serving commit, and dry-run choice argv
    carries.

    The expected serving commit is an argument, not an environment variable,
    because a caller cannot know which pipeline its target checkout carries: a
    pipeline that predates the bind refuses an argument it does not know, where
    it would ignore a variable and deploy unbound.
    """

    env: str
    sha: str
    dry_run: bool
    expected_serving_sha: str = ""


def parse_args(argv: Sequence[str]) -> Invocation:
    """`<staging|production> [--sha SHA] [--expected-serving-sha SHA] [--dry-run]`.

    `--sha` with nothing after it leaves the sha unset and takes the git
    default, rather than refusing: a trailing flag is a wrapper passing through
    an unset variable, and the default is the value it was reaching for.
    """
    env = ""
    sha = ""
    expected_serving_sha = ""
    dry_run = False
    index = 0
    while index < len(argv):
        argument = argv[index]
        if argument in ENVIRONMENTS:
            env = argument
        elif argument == "--sha":
            index += 1
            sha = argv[index] if index < len(argv) else ""
        elif argument == "--expected-serving-sha":
            # Unlike --sha, an empty value has no default to fall back on: read
            # as "no expected build", a trailing or blanked flag would turn the
            # bind off, and a repeat would replace the value the caller checked.
            index += 1
            if index >= len(argv) or not argv[index] or expected_serving_sha:
                raise UsageError("--expected-serving-sha takes one commit, once")
            expected_serving_sha = argv[index]
        elif argument == "--dry-run":
            dry_run = True
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
        dry_run=dry_run,
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
    dry_run: bool
    mode: str
    db_name: str
    wrangler_env: str
    wrangler: tuple[str, ...]
    drain_seconds: int
    allow_staging_bootstrap: bool
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
    retry_backoff: tuple[int, ...]
    verify_backoff: tuple[int, ...]
    preflight_cmd: tuple[str, ...] | None
    migrate_cmd: tuple[str, ...]
    postflight_cmd: tuple[str, ...] | None
    # The relocation body's own config, and empty on every other body. There
    # `db_name` is the TARGET - the database this checkout deploys against, as
    # on every body - and this names the SOURCE the Worker is being moved off.
    relocation_source_db: str
    relocate_copy_cmd: tuple[str, ...] | None
    # The operator's statement that the source is ALREADY fenced by an earlier
    # relocation run and this one continues it. A set lock cannot say who set
    # it, and a deploy that died inside its maintenance window leaves the same
    # bit; deploy_relocation.start_from has what each combination means.
    relocation_resume: bool
    # Whole seconds from the fence to the promote. 0 outside a relocation.
    relocation_max_pause_seconds: int
    github_actions: bool
    github_step_summary: str | None

    @property
    def is_production(self) -> bool:
        return self.env == "production"


def _relocation(
    environ: Mapping[str, str], *, suffix: str, db_name: str
) -> tuple[str, tuple[str, ...]]:
    """The relocation body's source database and copy hook, or a refusal.

    Each is required here for the reason the migrate hook is: the copy hook is
    reached only once the source is fenced, and discovering it missing there
    costs a write pause that bought nothing.
    """
    source = environ.get(f"DEPLOY_RELOCATION_SOURCE_DB_{suffix}", "")
    if not source:
        raise ConfigError(
            f"deploy-lib: DEPLOY_RELOCATION_SOURCE_DB_{suffix} is not set. A "
            "relocation moves the Worker off a source database onto "
            f"DEPLOY_DB_{suffix}; name the source."
        )
    if source == db_name:
        # One database named as the source and as the target would be fenced and
        # then opened, by a run that copied nothing.
        raise ConfigError(
            f"deploy-lib: DEPLOY_RELOCATION_SOURCE_DB_{suffix} and "
            f"DEPLOY_DB_{suffix} each name '{db_name}'; a relocation needs a "
            "source that is not its target"
        )
    copy_cmd = _hook(environ.get("RELOCATE_COPY_CMD"))
    if copy_cmd is None:
        raise ConfigError(
            "deploy-lib: RELOCATE_COPY_CMD is not set. The relocation body runs "
            "it once the source is fenced, with the environment name as its last "
            "argument; it must leave the target an exact, still-locked copy of "
            "the source, or fail."
        )
    return source, copy_cmd


def resolve(invocation: Invocation, environ: Mapping[str, str]) -> DeployConfig:
    """Read the environment into a DeployConfig, refusing what cannot deploy.

    Every refusal here happens before the first wrangler call, which is the
    point: an unrecognized mode, a missing database name, or an absent migrate
    hook must not be discovered after a maintenance version is already serving.
    """
    mode = environ.get("DEPLOY_PIPELINE_MODE") or WINDOWED
    if mode not in MODES:
        raise ConfigError(
            f"deploy-lib: unrecognized DEPLOY_PIPELINE_MODE '{mode}' "
            f"(expected '{WINDOWED}', '{WINDOWLESS}' or '{RELOCATION}')"
        )

    suffix = invocation.env.upper()
    db_name = environ.get(f"DEPLOY_DB_{suffix}", "")
    wrangler_env = environ.get(f"DEPLOY_WRANGLER_ENV_{suffix}", "")
    if not db_name or not wrangler_env:
        raise ConfigError(
            f"deploy-lib: missing config for env={invocation.env}\n"
            "  set DEPLOY_DB_STAGING / DEPLOY_DB_PRODUCTION and\n"
            "  DEPLOY_WRANGLER_ENV_STAGING / DEPLOY_WRANGLER_ENV_PRODUCTION "
            "in the pipeline's environment"
        )

    # The migrate hook is required, and it is required HERE rather than at the
    # step that runs it. On the windowed body that step is reached only after the
    # maintenance version is serving and the database is locked, so discovering
    # the hook missing there costs a consumer the full recovery path. The
    # value is known before anything runs, so the refusal belongs there.
    migrate_cmd = _hook(environ.get("MIGRATE_CMD"))
    if migrate_cmd is None:
        raise ConfigError(
            "deploy-lib: MIGRATE_CMD is not set. The pipeline runs the migrate "
            "hook as a command with the environment name as its last argument; "
            "set MIGRATE_CMD to that command (use `true` for a deploy that "
            "applies no migration)."
        )

    relocation_source_db = ""
    relocate_copy_cmd = None
    relocation_max_pause_seconds = 0
    if mode == RELOCATION:
        relocation_source_db, relocate_copy_cmd = _relocation(
            environ, suffix=suffix, db_name=db_name
        )
        relocation_max_pause_seconds = _whole_seconds(
            environ.get("DEPLOY_RELOCATION_MAX_PAUSE_SECONDS")
            or str(DEFAULT_RELOCATION_MAX_PAUSE_SECONDS),
            "DEPLOY_RELOCATION_MAX_PAUSE_SECONDS",
        )
        if relocation_max_pause_seconds == 0:
            # Zero would refuse every fenced step the instant the fence closed:
            # a relocation that pauses writes and can never do anything else.
            raise ConfigError(
                "DEPLOY_RELOCATION_MAX_PAUSE_SECONDS must be at least 1, got 0"
            )

    drain_seconds = _whole_seconds(
        environ.get("DEPLOY_DRAIN_SECONDS") or str(DEFAULT_DRAIN_SECONDS),
        "DEPLOY_DRAIN_SECONDS",
    )

    allow_staging_bootstrap = environ.get("DEPLOY_ALLOW_STAGING_BOOTSTRAP") == "true"
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
    if expected_serving_sha and allow_staging_bootstrap:
        # A bootstrap tolerates finding nothing serving, which no expected
        # build can be.
        raise ConfigError(
            "deploy-lib: --expected-serving-sha and "
            "DEPLOY_ALLOW_STAGING_BOOTSTRAP cannot be set together"
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
        dry_run=invocation.dry_run,
        mode=mode,
        db_name=db_name,
        wrangler_env=wrangler_env,
        wrangler=tuple(shlex.split(environ.get("WRANGLER") or DEFAULT_WRANGLER)),
        drain_seconds=drain_seconds,
        allow_staging_bootstrap=allow_staging_bootstrap,
        version_message_annotation=environ.get("DEPLOY_VERSION_MESSAGE_ANNOTATION", ""),
        commit_sha=commit_sha,
        expected_serving_sha=expected_serving_sha,
        expected_runtime=expected_runtime,
        diagnostics_dir=diagnostics_dir,
        retry_backoff=_backoff(
            environ.get("DEPLOY_WRANGLER_RETRY_BACKOFF_SECONDS")
            or DEFAULT_RETRY_BACKOFF,
            "DEPLOY_WRANGLER_RETRY_BACKOFF_SECONDS",
        ),
        verify_backoff=_backoff(
            environ.get("DEPLOY_VERIFY_BACKOFF_SECONDS") or DEFAULT_VERIFY_BACKOFF,
            "DEPLOY_VERIFY_BACKOFF_SECONDS",
        ),
        preflight_cmd=_hook(environ.get("PREFLIGHT_CMD")),
        migrate_cmd=migrate_cmd,
        postflight_cmd=_hook(environ.get("POSTFLIGHT_CMD")),
        relocation_source_db=relocation_source_db,
        relocate_copy_cmd=relocate_copy_cmd,
        relocation_resume=(
            mode == RELOCATION and environ.get("DEPLOY_RELOCATION_RESUME") == "true"
        ),
        relocation_max_pause_seconds=relocation_max_pause_seconds,
        github_actions=bool(environ.get("GITHUB_ACTIONS")),
        github_step_summary=environ.get("GITHUB_STEP_SUMMARY") or None,
    )
