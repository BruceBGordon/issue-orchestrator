"""Public-boundary test doubles for terminal process-group ownership."""

from __future__ import annotations

from dataclasses import dataclass, field

from issue_orchestrator.domain.terminal_session_termination import (
    TerminalSessionOwnerCancellation,
)
from tests.process_tree_fixture import DirectChildProcessCohort


@dataclass(slots=True)
class RecordingTerminalSessionLaunchLease:
    """Prepared test launch with explicit readiness and retirement calls."""

    command: tuple[str, ...]
    cancellation: TerminalSessionOwnerCancellation
    ready_cancellations: list[TerminalSessionOwnerCancellation]
    retired_cancellations: list[TerminalSessionOwnerCancellation]
    abandoned_cancellations: list[TerminalSessionOwnerCancellation]

    @property
    def inherited_file_descriptors(self) -> tuple[int, ...]:
        return ()

    def require_ready(self) -> None:
        self.ready_cancellations.append(self.cancellation)

    def retire_after_containment(self) -> None:
        self.retired_cancellations.append(self.cancellation)

    def abandon_after_spawn_uncertainty(self) -> None:
        self.abandoned_cancellations.append(self.cancellation)


@dataclass(slots=True)
class RecordingTerminalSessionOwner:
    """Record typed launch preparation while preserving the command."""

    prepared_commands: list[tuple[str, ...]] = field(default_factory=list)
    ready_cancellations: list[TerminalSessionOwnerCancellation] = field(
        default_factory=list
    )
    retired_cancellations: list[TerminalSessionOwnerCancellation] = field(
        default_factory=list
    )
    abandoned_cancellations: list[TerminalSessionOwnerCancellation] = field(
        default_factory=list
    )

    def prepare(
        self,
        command: tuple[str, ...],
        cancellation: TerminalSessionOwnerCancellation,
    ) -> RecordingTerminalSessionLaunchLease:
        self.prepared_commands.append(command)
        return RecordingTerminalSessionLaunchLease(
            command,
            cancellation,
            self.ready_cancellations,
            self.retired_cancellations,
            self.abandoned_cancellations,
        )


@dataclass(frozen=True, slots=True)
class TerminalSessionSentinelCohort:
    """Exact redundant sentinel identities hidden behind a behavior test API."""

    children: DirectChildProcessCohort

    @classmethod
    def observe(cls, terminal_process_id: int) -> TerminalSessionSentinelCohort:
        return cls(
            DirectChildProcessCohort.observe_exact(
                parent_process_id=terminal_process_id,
                module_name="issue_orchestrator.execution.process_group_sentinel",
                expected_count=2,
            )
        )

    def crash_one(self) -> None:
        self.children.crash_one()
