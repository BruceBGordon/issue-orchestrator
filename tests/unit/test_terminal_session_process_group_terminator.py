"""Exact-identity terminal process-group containment tests."""

from __future__ import annotations

from pathlib import Path
import signal

import pytest

from issue_orchestrator.domain.executor import (
    ExecutorInteractiveSessionCancellation,
)
from issue_orchestrator.domain.process_group import (
    ProcessBirthIdentity,
    ProcessGroupAbsent,
    ProcessGroupExecutable,
    ProcessGroupObservation,
    ProcessGroupPermissionDenied,
    ProcessGroupZombiesOnly,
    ProcessIdentityAbsent,
    ProcessIdentityObservation,
    ProcessIdentityPermissionDenied,
    ProcessIdentityPresent,
    ProcessSessionLeaderAbsent,
    ProcessSessionLeaderPresent,
    ProcessSessionObservation,
)
from issue_orchestrator.domain.terminal_session_termination import (
    TerminalSessionProcess,
    TerminalSessionStatus,
    TerminalSessionTerminationOutcome,
    TerminalSessionTerminationPolicy,
)
from issue_orchestrator.execution.executor_guardian_cancellation import (
    ExecutorSessionGuardianCanceller,
)
from issue_orchestrator.execution.session_process_group_terminator import (
    PosixTerminalSessionProcessGroupTerminator,
    TerminalSessionContainmentError,
)
from issue_orchestrator.ports.process_group_observer import ProcessGroupObserver
from tests.unit.process_group_observer_helpers import RecordingProcessGroupObserver


class _LeaderDisappearsBeforeReusedGroupObserver:
    """Expose a group reuse only after the recorded leader disappears."""

    def __init__(self) -> None:
        self._session_observations = 0

    def observe_process(self, process_id: int) -> ProcessIdentityObservation:
        del process_id
        return ProcessIdentityAbsent()

    def observe_group(self, process_group_id: int) -> ProcessGroupObservation:
        del process_group_id
        return ProcessGroupExecutable(1)

    def observe_session(
        self,
        process_id: int,
        expected_birth_identity: ProcessBirthIdentity,
    ) -> ProcessSessionObservation:
        del expected_birth_identity
        self._session_observations += 1
        if self._session_observations == 1:
            return ProcessSessionLeaderPresent(
                ProcessIdentityPresent(
                    ProcessBirthIdentity("darwin-timeval:1700000000:100"),
                    process_id,
                ),
                ProcessGroupExecutable(1),
            )
        return ProcessSessionLeaderAbsent()


def _process(tmp_path: Path) -> TerminalSessionProcess:
    return TerminalSessionProcess(
        process_id=42,
        birth_identity=ProcessBirthIdentity("darwin-timeval:1700000000:100"),
        executor_cancellation=ExecutorInteractiveSessionCancellation.for_run_dir(
            tmp_path.resolve()
        ),
    )


def _terminator(
    observer: ProcessGroupObserver,
) -> PosixTerminalSessionProcessGroupTerminator:
    return PosixTerminalSessionProcessGroupTerminator(
        TerminalSessionTerminationPolicy(0.1, 0.1),
        ExecutorSessionGuardianCanceller(0.1, observer),
        observer,
    )


def test_recycled_pid_is_retired_without_signalling(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observer = RecordingProcessGroupObserver(
        process_observation=ProcessIdentityPresent(
            ProcessBirthIdentity("darwin-timeval:1700000000:200"),
            42,
        )
    )
    monkeypatch.setattr(
        "issue_orchestrator.execution.session_process_group_terminator.os.killpg",
        lambda *_arguments: pytest.fail("a recycled PID must never be signalled"),
    )

    outcome = _terminator(observer).terminate(_process(tmp_path))

    assert outcome is TerminalSessionTerminationOutcome.STALE_IDENTITY_RETIRED
    assert observer.process_group_ids == []


def test_zombie_only_group_is_contained(tmp_path: Path) -> None:
    observer = RecordingProcessGroupObserver(
        process_observation=ProcessIdentityPresent(
            ProcessBirthIdentity("darwin-timeval:1700000000:100"),
            42,
        ),
        group_observation=ProcessGroupZombiesOnly(2),
    )

    assert _terminator(observer).status(_process(tmp_path)) is (
        TerminalSessionStatus.CONTAINED
    )


def test_absent_leader_prevents_signalling_a_reused_group(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observer = _LeaderDisappearsBeforeReusedGroupObserver()
    signals: list[signal.Signals] = []

    def record_signal(process_group_id: int, signal_number: signal.Signals) -> None:
        assert process_group_id == 42
        signals.append(signal_number)

    monkeypatch.setattr(
        "issue_orchestrator.execution.session_process_group_terminator.os.killpg",
        record_signal,
    )

    outcome = _terminator(observer).terminate(_process(tmp_path))

    assert outcome is TerminalSessionTerminationOutcome.STALE_IDENTITY_RETIRED
    assert signals == [signal.SIGTERM]

@pytest.mark.parametrize(
    ("observer", "detail"),
    (
        (
            RecordingProcessGroupObserver(
                process_observation=ProcessIdentityPermissionDenied("process denied")
            ),
            "process denied",
        ),
        (
            RecordingProcessGroupObserver(
                process_observation=ProcessIdentityPresent(
                    ProcessBirthIdentity("darwin-timeval:1700000000:100"),
                    42,
                ),
                group_observation=ProcessGroupPermissionDenied("group denied"),
            ),
            "group denied",
        ),
    ),
)
def test_permission_denial_never_degrades_to_contained(
    tmp_path: Path,
    observer: RecordingProcessGroupObserver,
    detail: str,
) -> None:
    with pytest.raises(TerminalSessionContainmentError, match=detail):
        _terminator(observer).status(_process(tmp_path))
