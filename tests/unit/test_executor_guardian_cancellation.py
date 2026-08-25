"""Ownership-boundary tests for terminal-to-guardian cancellation."""

from __future__ import annotations

from pathlib import Path
import math

import pytest

from issue_orchestrator.domain.executor import (
    ExecutorInteractiveSessionCancellation,
)
from issue_orchestrator.execution.executor_guardian_cancellation import (
    ExecutorGuardianCancellationError,
    ExecutorGuardianCancellationOutcome,
    ExecutorSessionGuardianCanceller,
    InteractiveExecutorGuardianCancellationLease,
)
from tests.unit.process_group_observer_helpers import RecordingProcessGroupObserver


def _cancellation(run_dir: Path) -> ExecutorInteractiveSessionCancellation:
    return ExecutorInteractiveSessionCancellation.for_run_dir(run_dir.resolve())


def _canceller(seconds: float) -> ExecutorSessionGuardianCanceller:
    return ExecutorSessionGuardianCanceller(
        seconds,
        RecordingProcessGroupObserver(),
    )


@pytest.mark.parametrize("seconds", (0.0, math.nan, math.inf))
def test_canceller_requires_a_finite_positive_force_bound(seconds: float) -> None:
    with pytest.raises(ValueError, match="must be positive"):
        _canceller(seconds)


def test_canceller_reports_an_absent_endpoint_explicitly(tmp_path: Path) -> None:
    outcome = _canceller(1.0).contain_if_active(
        _cancellation(tmp_path)
    )

    assert outcome is ExecutorGuardianCancellationOutcome.ABSENT


def test_canceller_validates_and_retires_a_stale_record(tmp_path: Path) -> None:
    cancellation = _cancellation(tmp_path)
    cancellation.record_path.write_text(
        '{"schema_version":1,"process_group_id":12345}\n',
        encoding="utf-8",
    )

    outcome = _canceller(1.0).contain_if_active(cancellation)

    assert outcome is ExecutorGuardianCancellationOutcome.STALE_RETIRED
    assert not cancellation.record_path.exists()


def test_canceller_fails_fast_on_a_malformed_stale_record(tmp_path: Path) -> None:
    cancellation = _cancellation(tmp_path)
    cancellation.record_path.write_text(
        '{"process_group_id":"wrong"}\n', encoding="utf-8"
    )

    with pytest.raises(
        ExecutorGuardianCancellationError,
        match="invalid executor guardian cancellation record",
    ):
        _canceller(1.0).contain_if_active(cancellation)


def test_only_one_guardian_can_own_a_run_cancellation_endpoint(
    tmp_path: Path,
) -> None:
    cancellation = _cancellation(tmp_path)
    owner = InteractiveExecutorGuardianCancellationLease(cancellation)
    try:
        with pytest.raises(
            ExecutorGuardianCancellationError,
            match="already owns this run",
        ):
            InteractiveExecutorGuardianCancellationLease(cancellation)
    finally:
        owner.retire()
