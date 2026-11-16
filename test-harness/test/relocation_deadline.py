"""Order relocation expiry or startup cancellation after the hook is ready.

The bats cases own the recovery assertions. This driver intercepts the real
subprocess boundary after its PID record exists, so scheduler delays cannot
prevent the cleanup fixture from starting.
"""

from __future__ import annotations

import os
import pathlib
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from unittest.mock import patch

TEST_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(TEST_DIR.parents[1] / "deploy-lib"))

import safe_deploy_pipeline

# A missing fixture must fail before the CI job's backstop. This bounds setup,
# never the timeout assertion, which is driven by readiness instead of time.
INFINITE = 120
REAL_WAIT = subprocess.Popen.wait


def is_hanging_hook(process: subprocess.Popen[bytes]) -> bool:
    args = process.args
    # The vendored smoke runs under /var on macOS, an alias of /private/var.
    return (
        isinstance(args, (list, tuple))
        and bool(args)
        and isinstance(args[0], str)
        and pathlib.Path(args[0]).resolve() == TEST_DIR / "example-hook.sh"
        and any(label in args for label in ("hang", "hang-ignoring-sigterm"))
    )


def await_readiness(process: subprocess.Popen[bytes]) -> None:
    ready = pathlib.Path(os.environ["EXAMPLE_HOOK_PID_FILE"])
    give_up = time.monotonic() + INFINITE
    while process.poll() is None:
        if ready.exists():
            pids = ready.read_text().split()
            if len(pids) == 2 and pids[0] == str(process.pid):
                return
        if time.monotonic() >= give_up:
            safe_deploy_pipeline._stop_group(process)
            raise RuntimeError("hanging hook never published its PID record")
        time.sleep(0.01)
    raise RuntimeError("hanging hook exited before publishing its PID record")


def wait_after_readiness(
    process: subprocess.Popen[bytes], timeout: float | None = None
) -> int:
    if timeout is not None and is_hanging_hook(process):
        await_readiness(process)
        # Use the real TimeoutExpired path and production group cleanup; no
        # simulated return code can satisfy the test.
        print("[deadline-fixture] hook ready; expiring wait", flush=True)
        return REAL_WAIT(process, timeout=0)
    return REAL_WAIT(process, timeout=timeout)


def cancel_before_adoption[**P](
    start: Callable[P, subprocess.Popen[bytes]],
) -> Callable[P, subprocess.Popen[bytes]]:
    def spawn(*args: P.args, **kwargs: P.kwargs) -> subprocess.Popen[bytes]:
        process = start(*args, **kwargs)
        if is_hanging_hook(process):
            await_readiness(process)
            # The child exists, but production has not received its handle.
            # A default signal disposition here used to orphan its group.
            print("[deadline-fixture] hook ready; cancelling startup", flush=True)
            os.kill(os.getpid(), signal.SIGTERM)
        return process

    return spawn


def main() -> int:
    os.environ.update(
        DEPLOY_DB_STAGING="example-db-staging",
        DEPLOY_WRANGLER_ENV_STAGING="staging",
        DEPLOY_RELOCATION_MAX_PAUSE_SECONDS=str(INFINITE),
    )
    if os.environ.get("EXAMPLE_CANCEL_STARTUP") == "1":
        start: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen
        with patch.object(subprocess, "Popen", cancel_before_adoption(start)):
            return safe_deploy_pipeline.main(["staging", "--sha", "relocationtest"])
    with patch.object(subprocess.Popen, "wait", wait_after_readiness):
        return safe_deploy_pipeline.main(["staging", "--sha", "relocationtest"])


if __name__ == "__main__":
    sys.exit(main())
