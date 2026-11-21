"""The captured serving version, promotion outcome and rollback diagnostics."""

from __future__ import annotations

import dataclasses


class DeployAbort(Exception):
    """A step refused to continue. Recovery still runs; this carries the status."""

    def __init__(self, exit_code: int = 1) -> None:
        super().__init__(f"deploy aborted with exit code {exit_code}")
        self.exit_code = exit_code


@dataclasses.dataclass
class PipelineState:
    """One run's mutable state, shared by the steps and the recovery path."""

    prev_version_id: str = ""
    new_final_id: str = ""
    # A promotion can fail locally after applying remotely. Clear on success
    # so a later postflight failure leaves the new version serving.
    final_promote_attempted: bool = False
    # The single serving version the windowless capture recorded: the reference
    # every TOCTOU re-check matches against. Identical to prev_version_id at
    # capture time, kept under its own name so the re-checks read as what they
    # are rather than as a rollback concern.
    serving_version_id: str = ""

    rollback_result: str = "not_needed"
    failed_step: str = "none"

    def uploaded_version(self, version_id: str) -> bool:
        """Only an id returned by this run can still be propagating."""
        return bool(version_id) and version_id == self.new_final_id
