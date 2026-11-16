"""The deploy's operator-facing output: step groups, timings, and the summary.

One object owns the current group, the step's start and the deploy's start,
because those three are only ever read together: a step boundary closes the
previous group, prints how long it took, and appends that row to the GitHub step
summary. Nothing else in the pipeline touches them.

The audiences, deliberately kept apart. Stdout and stderr are what an operator
reads in a CI log, and every write is flushed so the pipeline's own lines stay
interleaved with the wrangler output they describe - a Python stdout attached to
a file or a pipe is block-buffered, so without the flush the whole transcript
would arrive at the end, in the wrong order relative to stderr. Neither stream
may raise on the text it is handed: stdout and stderr render an unencodable byte rather than
refusing it, which `configure_stdout` below owns. GITHUB_STEP_SUMMARY is the run
page's table, appended to rather than streamed, and best-effort.

`ci_error` is the one line shaped by where it runs: inside GitHub Actions it
carries the `::error::` workflow command so the failure surfaces on the run
page, and outside it carries `[FAIL] ` so a local run does not print workflow
syntax at a human.
"""

from __future__ import annotations

import pathlib
import sys
import time

SEPARATOR = "------------------------------------------"


def configure_stdout() -> None:
    """Make stdout render bytes it cannot encode instead of raising on them.

    Called once, at the process entry point, because the stream's error handler
    is where this is decided - not a guard around each `emit`.

    Why it is needed: wrangler_runner decodes the API's streams with
    surrogateescape, deliberately, so the transient classifier sees the text
    Cloudflare actually sent. A byte that is not UTF-8 therefore reaches the
    version ids, bookmarks and lock states this module prints, and Python's
    stdout is `strict` by default - so printing one raised UnicodeEncodeError
    and took the deploy with it, at whichever step did the printing. The
    bookmark line runs after the promote and after the lock, which put that
    abort past the point of no return.

    `backslashreplace` is what stderr already uses (Python's own default, and
    unchanged by PYTHONIOENCODING), so stdout and stderr agree and the operator
    reads the byte in its escaped form rather than losing the line. The value
    itself is untouched: it still reaches wrangler as the original bytes,
    because argv is encoded with surrogateescape.
    """
    # Guarded because a caller may have replaced the stream with something that
    # is not a TextIOWrapper; a log setting is not worth an AttributeError.
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(errors="backslashreplace")


def emit(line: str = "") -> None:
    """One line to stdout, flushed. See the buffering note in the docstring."""
    print(line, flush=True)


def emit_error(line: str) -> None:
    """One line to stderr, flushed."""
    print(line, file=sys.stderr, flush=True)


class Reporter:
    """Step grouping, per-step timing, and the run's summary table."""

    def __init__(self, *, github_actions: bool, step_summary_path: str | None) -> None:
        self._github_actions = github_actions
        self._step_summary_path = step_summary_path
        self._summary_broken = False
        self._current_group = ""
        self._step_start: float | None = None
        self._deploy_start: float | None = None

    @property
    def current_group(self) -> str:
        """The step that is running, which is what a failure is attributed to."""
        return self._current_group

    def start_deploy(self) -> None:
        """Mark the run's start, which the closing total is measured against."""
        self._deploy_start = time.time()

    def summary(self, line: str) -> None:
        """Append one line to the GitHub step summary, best-effort.

        The summary is operator-facing logging and must never take the recovery
        path with it: an unwritable file raising out of here would abort the
        pipeline mid-recovery - between the failure report and the D1 restore,
        worker rollback and unlock that follow it - leaving the product on the
        maintenance version with the lock still set. The first failure reports
        once, to stderr and never back through here, and turns the writer off
        for the rest of the run, because every remaining line would fail the
        same way and repeat the same message.
        """
        if self._step_summary_path is None or self._summary_broken:
            return
        try:
            with pathlib.Path(self._step_summary_path).open(
                "a", encoding="utf-8"
            ) as sink:
                sink.write(f"{line}\n")
        except Exception as exc:  # noqa: BLE001 - logging never breaks recovery
            # Broad on purpose, like the recovery layers' own catch: the class
            # is "this line could not be written", and it has more members than
            # an unwritable file. wrangler_runner decodes the API's bytes with
            # surrogateescape, so a version id or bookmark the summary reports
            # can carry a lone surrogate, and encoding one raises
            # UnicodeEncodeError - a ValueError, which no OSError catch sees.
            # Enumerating members is what left the original write exposed.
            self._summary_broken = True
            self.ci_error(
                f"GitHub step summary write failed ({exc}) - the run page table "
                "stops here. The deploy and its recovery are unaffected."
            )

    def ci_error(self, message: str) -> None:
        """An error line the CI run page surfaces, or a plain [FAIL] locally."""
        if self._github_actions:
            emit_error(f"::error::{message}")
        else:
            emit_error(f"[FAIL] {message}")

    def step(self, label: str) -> None:
        """Close the previous step, then open this one."""
        self._close_step()
        self._current_group = label
        self._step_start = time.time()
        if self._github_actions:
            emit(f"::group::{label}")
        emit("")
        emit(f"> {label}")
        emit(SEPARATOR)

    def end_steps(self) -> None:
        """Close the last open step. Idempotent - cleanup calls it after a body did."""
        self._close_step()
        self._current_group = ""

    def total_seconds(self) -> int | None:
        """How long the run took, or None when it never started."""
        if self._deploy_start is None:
            return None
        return int(time.time() - self._deploy_start)

    def _close_step(self) -> None:
        if self._step_start is not None:
            elapsed = int(time.time() - self._step_start)
            label = self._current_group or "step"
            emit(f"  [time] {label} completed in {elapsed}s")
            self.summary(f"| {label} | {elapsed}s |")
            self._step_start = None
        if self._current_group and self._github_actions:
            emit("::endgroup::")
