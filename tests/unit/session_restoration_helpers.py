"""Typed session-restoration containment fakes."""

from __future__ import annotations

from dataclasses import dataclass, field

from issue_orchestrator.control.session_restorer import SessionRestorer
from issue_orchestrator.domain.session_restoration import UnsupportedSessionRun
from issue_orchestrator.ports.unsupported_session_run_containment import (
    UnsupportedSessionRunContainment,
)


class RejectUnsupportedSessionRuns:
    """Fail a test that unexpectedly discovers an unsupported live run."""

    def contain(self, run: UnsupportedSessionRun) -> None:
        raise AssertionError(f"unexpected unsupported live run: {run!r}")


REJECT_UNSUPPORTED_SESSION_RUNS = RejectUnsupportedSessionRuns()


@dataclass(slots=True)
class RecordingUnsupportedSessionRunContainment:
    """Record each unsupported live run the restoration owner contains."""

    runs: list[UnsupportedSessionRun] = field(default_factory=list)

    def contain(self, run: UnsupportedSessionRun) -> None:
        if type(run) is not UnsupportedSessionRun:
            raise ValueError("contain requires UnsupportedSessionRun")
        self.runs.append(run)


def make_session_restorer(
    config: object,
    repository_host: object,
    working_copy: object,
    tech_lead_authority: object | None = None,
    *,
    containment: UnsupportedSessionRunContainment = REJECT_UNSUPPORTED_SESSION_RUNS,
) -> SessionRestorer:
    """Build the production owner with an explicit test containment boundary."""
    return SessionRestorer(  # pyright: ignore[reportArgumentType]
        config=config,
        repository_host=repository_host,
        working_copy=working_copy,
        unsupported_session_run_containment=containment,
        tech_lead_authority=tech_lead_authority,
    )
