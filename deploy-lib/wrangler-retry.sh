# shellcheck shell=sh
#
# A wrangler runner that retries against the transient roster, plus the shell
# entry point to that roster. Sourced (not executed) by the consumer's own
# deploy scripts - the hook and helper scripts a deploy wrapper points at, which
# reach Cloudflare from outside the pipeline's own process. The roster itself is
# wrangler_transient.py in this directory, which owns which error shapes are
# worth another attempt and why.
#
# Why one shared roster rather than a copy beside each caller: a deploy reaches
# Cloudflare from several places, and a retry gap is invisible until a live
# blip - the pipeline retried its own wrangler calls while the preflight's reads
# carried no retry at all, so one `fetch failed` reading bootstrap rows aborted
# a production deploy the rest of the pipeline would have ridden out. One roster
# means adding an error shape covers every caller at once instead of the subset
# whose copy someone remembered to edit.
#
# Scope is idempotent READS. A write needs its own idempotency analysis before
# it may retry, and the deploy path's writes already carry one: the schema and
# stats imports retry under their own bounded loops, which re-read live state
# and classify where the import landed before re-attempting, and the rollback
# floor's advance is monotonic by construction with a read-back after it.
#
# Function definitions plus one startup precondition block, and no other
# top-level side effects, so sourcing this file is safe from inside another
# script's `set -e` context. The preconditions are checked here rather than at
# first use because the only thing a classifier that cannot run can do mid-deploy
# is decline to retry, which looks exactly like a deploy that had no blips.

# === Constraint: locating the roster ===
# POSIX sh gives a sourced file no way to locate its own directory, so the
# consumer supplies the toolkit root, the same way it supplies this file's path.
if [ -z "${DEPLOY_TOOLKIT_DIR:-}" ]; then
    echo "[FAIL] wrangler-retry: set DEPLOY_TOOLKIT_DIR to the toolkit root before sourcing this file" >&2
    exit 1
fi

DEPLOY_TRANSIENT_CLASSIFIER="$DEPLOY_TOOLKIT_DIR/deploy-lib/wrangler_transient.py"
if [ ! -f "$DEPLOY_TRANSIENT_CLASSIFIER" ]; then
    echo "[FAIL] wrangler-retry: no transient roster at $DEPLOY_TRANSIENT_CLASSIFIER" >&2
    echo "       (DEPLOY_TOOLKIT_DIR should be the directory holding deploy-lib/)" >&2
    exit 1
fi

# Named version first, then the bare name: `python3` can resolve below the floor
# the toolkit's Python is written against - Apple's /usr/bin/python3 is the common
# case - and a vendored copy of the toolkit gets the same
# ladder rather than any one repo's virtualenv.
#
# The name is toolkit-wide rather than private to this file because sourcing it
# is how a consumer's script gets a resolved interpreter at all: the ladder runs
# once, here, and every later decision the consumer reads goes through the same
# answer. Repeating the ladder would create competing answers on a host part-way through
# provisioning.
if command -v python3.14 >/dev/null 2>&1; then
    DEPLOY_TOOLKIT_PYTHON=python3.14
elif command -v python3 >/dev/null 2>&1; then
    DEPLOY_TOOLKIT_PYTHON=python3
else
    echo "[FAIL] wrangler-retry: no python3 on PATH; the toolkit's decisions cannot be read" >&2
    exit 1
fi

# Classify wrangler output as transient-and-retryable or not. Returns 0 for
# transient and 1 for anything else, so a caller reads it as a plain condition.
deploy_output_is_transient() {
    _dwt_rc=0
    # A pipe is safe into this particular reader: it drains stdin to EOF before
    # deciding anything, so the writer cannot fault on a reader that left early
    # and no pipefail context can turn that fault into the verdict. The suite beside the harness pins the
    # drain, because it is what makes this line correct.
    printf '%s' "$1" | "$DEPLOY_TOOLKIT_PYTHON" "$DEPLOY_TRANSIENT_CLASSIFIER" || _dwt_rc=$?

    case "$_dwt_rc" in
    0) return 0 ;;
    1) return 1 ;;
    esac

    # Any other status means the classifier never reached a verdict. Say so
    # loudly and decline the retry, which is the direction the allowlist errs in
    # everywhere else; a quiet "not transient" would turn a broken classifier
    # into a deploy that had simply stopped riding out blips, with nothing in
    # the log to say so.
    echo "[FAIL] wrangler-retry: transient classifier exited $_dwt_rc, treating output as non-retryable" >&2
    return 1
}

# deploy_wrangler_retry <wrangler-arg>...
#
# Runs `$WRANGLER "$@"` from $SERVICE_DIR, retrying only failures
# deploy_output_is_transient accepts. Returns wrangler's exit status and sets:
#
#   DEPLOY_WRANGLER_STDOUT   the terminal attempt's stdout
#   DEPLOY_WRANGLER_STDERR   the terminal attempt's stderr
#
# The stdout and stderr streams stay separate, and retry diagnostics go to this shell's
# stderr rather than into either of them, so a caller parsing JSON out of
# DEPLOY_WRANGLER_STDOUT can never find a retry line or a wrangler warning
# mixed into its document.
#
# Every verdict stays with the caller - envelope shape, sentinel rows, error
# wording, whether a failure is fatal - because the callers disagree on all of
# them (a missing table is fatal to the schema gate and an expected sentinel to
# the rollback floor). This decides one thing: whether a failure was worth
# another attempt.
#
# Backoff comes from DEPLOY_SCRIPT_RETRY_BACKOFF_SECONDS, a space-separated
# list; total attempts = items + 1. Set it to the empty string for no retries
# at all (the `-` expansion below distinguishes unset from empty, which is what
# lets a test ask for the pre-retry behavior). Fixed rather than jittered: a
# deploy is strictly serial, so there is no fleet of retriers to spread out -
# the schedule only has to outlast a control-plane blip.
deploy_wrangler_retry() {
    _dwr_schedule="${DEPLOY_SCRIPT_RETRY_BACKOFF_SECONDS-2 5 15}"
    _dwr_attempt=1

    while :; do
        _dwr_stdout_tmp=$(mktemp)
        _dwr_stderr_tmp=$(mktemp)
        _dwr_exit=0

        # shellcheck disable=SC2086
        # $WRANGLER is intentionally word-split ("npx wrangler"). CI=true
        # suppresses wrangler's "agent skills" banner, which prints to stdout on
        # agent runtimes and would land inside the JSON a caller parses out of
        # DEPLOY_WRANGLER_STDOUT. Every current caller also exports CI, so this
        # changes nothing today; it is here because the helper is general, and
        # the next caller should not have to know that a banner can corrupt its
        # read. Scoped to this process, like the pipeline's own helper does it.
        (
            cd "$SERVICE_DIR"
            CI=true $WRANGLER "$@"
        ) >"$_dwr_stdout_tmp" 2>"$_dwr_stderr_tmp" || _dwr_exit=$?

        DEPLOY_WRANGLER_STDOUT=$(cat "$_dwr_stdout_tmp")
        DEPLOY_WRANGLER_STDERR=$(cat "$_dwr_stderr_tmp")
        rm -f "$_dwr_stdout_tmp" "$_dwr_stderr_tmp"

        if [ "$_dwr_exit" -eq 0 ]; then
            if [ "$_dwr_attempt" -gt 1 ]; then
                echo "[wrangler-retry] $1: succeeded on attempt $_dwr_attempt" >&2
            fi
            return 0
        fi

        if ! deploy_output_is_transient "$DEPLOY_WRANGLER_STDOUT$DEPLOY_WRANGLER_STDERR"; then
            return "$_dwr_exit"
        fi

        if [ -z "$_dwr_schedule" ]; then
            if [ "$_dwr_attempt" -gt 1 ]; then
                echo "[wrangler-retry] $1: exhausted $_dwr_attempt attempts on transient errors (exit=$_dwr_exit)" >&2
            fi
            return "$_dwr_exit"
        fi

        _dwr_sleep="${_dwr_schedule%% *}"
        case "$_dwr_schedule" in
        *" "*) _dwr_schedule="${_dwr_schedule#* }" ;;
        *) _dwr_schedule="" ;;
        esac

        # Replay each attempt's stderr rather than only the last, so an operator
        # reading CI logs sees whether the blip changed shape between attempts.
        if [ -n "$DEPLOY_WRANGLER_STDERR" ]; then
            echo "$DEPLOY_WRANGLER_STDERR" >&2
        fi
        echo "[wrangler-retry] $1: attempt $_dwr_attempt failed with a transient error (exit=$_dwr_exit), retrying in ${_dwr_sleep}s" >&2
        sleep "$_dwr_sleep"
        _dwr_attempt=$((_dwr_attempt + 1))
    done
}

# deploy_d1_execute_json <query>
#
# `wrangler d1 execute --json` against the target the caller has already
# configured, through deploy_wrangler_retry. Sets DEPLOY_D1_STDOUT /
# DEPLOY_D1_STDERR and returns wrangler's exit status.
#
# Target config read from the caller's environment, the same names the deploy
# scripts already set: WRANGLER, SERVICE_DIR, DB_NAME, LOCAL_FLAG,
# LOCAL_PERSIST_TO, WRANGLER_ENV.
deploy_d1_execute_json() {
    _dd_query="$1"
    _dd_rc=0

    if [ -n "${LOCAL_PERSIST_TO:-}" ]; then
        # shellcheck disable=SC2086
        # $LOCAL_FLAG is empty or a single flag; intentionally word-split.
        deploy_wrangler_retry d1 execute "$DB_NAME" $LOCAL_FLAG \
            --persist-to "$LOCAL_PERSIST_TO" --json --command "$_dd_query" || _dd_rc=$?
    elif [ -z "${WRANGLER_ENV:-}" ]; then
        # shellcheck disable=SC2086
        deploy_wrangler_retry d1 execute "$DB_NAME" $LOCAL_FLAG \
            --json --command "$_dd_query" || _dd_rc=$?
    else
        # shellcheck disable=SC2086
        deploy_wrangler_retry d1 execute "$DB_NAME" $LOCAL_FLAG \
            --env "$WRANGLER_ENV" --json --command "$_dd_query" || _dd_rc=$?
    fi

    # shellcheck disable=SC2034
    # Read by the sourcing script, which shellcheck cannot follow from here.
    DEPLOY_D1_STDOUT="$DEPLOY_WRANGLER_STDOUT"
    # shellcheck disable=SC2034
    DEPLOY_D1_STDERR="$DEPLOY_WRANGLER_STDERR"
    return "$_dd_rc"
}
