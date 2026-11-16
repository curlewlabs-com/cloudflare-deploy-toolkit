#!/usr/bin/env python3
"""Suite for a cancellation that lands while the pipeline is writing to stderr.

A fenced hook runs in a process group of its own, so a signal ending the
pipeline has to stop that group itself. Python runs the handler between the
steps of whatever the main thread was doing, and a `print` mid-flush is one of
those steps: it holds stderr's buffered writer. A handler that did the stop
itself would re-enter that writer with its [STOP] line, and the RuntimeError
would end the handler before the stop - leaving the hook's group running
against the databases after the pipeline had gone, and the signal undelivered.

The bats cases in test-deploy-pipeline-relocation.bats signal a whole pipeline
from outside, which cannot choose the instant the signal lands. This case
chooses it: stderr's raw layer delivers the signal from inside the flush.
"""

from __future__ import annotations

import contextlib
import io
import os
import pathlib
import signal
import sys
import unittest
from collections.abc import Buffer
from types import FrameType
from typing import override

MODULE = (
    pathlib.Path(__file__).resolve().parents[2]
    / "deploy-lib"
    / "safe_deploy_pipeline.py"
)

sys.path.insert(0, str(MODULE.parent))

import safe_deploy_pipeline
from deploy_reporting import emit_error
from relocation_deadline import INFINITE

# Parks until the stop signals it. The suite runs with a PATH that holds no
# `sleep`; the interpreter running it is the one program certain to be there.
HOOK = [sys.executable, "-c", "import signal; signal.pause()"]


class SignallingStderr(io.RawIOBase):
    """Stderr's raw layer, which sends SIGTERM from inside its first armed write.

    The buffered writer above it holds its lock across this call, so the
    pipeline's handler runs while that lock is held - what a runner's
    cancellation does when it lands during a flush.
    """

    def __init__(self) -> None:
        super().__init__()
        self.written = bytearray()
        self.armed = False

    @override
    def writable(self) -> bool:
        return True

    @override
    def write(self, b: Buffer, /) -> int:
        self.written += b
        if self.armed:
            self.armed = False
            os.kill(os.getpid(), signal.SIGTERM)
        return memoryview(b).nbytes


class CancellationDuringAStderrWrite(unittest.TestCase):
    @override
    def setUp(self) -> None:
        # What the pipeline's previous disposition stands in for: delivering the
        # signal again must not end this test process.
        self.delivered: list[int] = []

        def record(signum: int, _frame: FrameType | None) -> None:
            self.delivered.append(signum)

        previous = signal.signal(signal.SIGTERM, record)
        self.addCleanup(signal.signal, signal.SIGTERM, previous)
        self.raw = SignallingStderr()
        stderr = io.TextIOWrapper(
            io.BufferedWriter(self.raw), encoding="utf-8", line_buffering=True
        )
        redirect = contextlib.redirect_stderr(stderr)
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)

    def leave_no_hook_behind(self, group: int) -> None:
        safe_deploy_pipeline._signal_group(group, signal.SIGKILL)

    def test_the_hook_is_stopped_and_the_signal_delivered_again(self) -> None:
        with safe_deploy_pipeline._stopping_the_hook_if_the_pipeline_is_ended(
            HOOK
        ) as hook:
            if hook is None:
                self.fail(self.raw.written.decode())
            self.addCleanup(self.leave_no_hook_behind, hook.process.pid)
            self.raw.armed = True
            emit_error("hook output")
            status = hook.wait(INFINITE)

        self.assertEqual(status, -signal.SIGTERM)
        self.assertEqual(self.delivered, [signal.SIGTERM])
        with self.assertRaises(ProcessLookupError):
            os.killpg(hook.process.pid, 0)  # noqa: TID251 - asserts ESRCH itself
        written = self.raw.written.decode()
        self.assertIn("hook output\n", written)
        self.assertIn(f"[STOP] signal {int(signal.SIGTERM)}", written)


if __name__ == "__main__":
    unittest.main()
