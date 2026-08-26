"""Ownership-boundary tests for terminal-to-guardian cancellation."""

from __future__ import annotations

from pathlib import Path
import math

import pytest

from issue_orchestrator.domain.executor import (
    ExecutorInteractiveSessionCancellation,
    ExecutorSessionContainmentOutcome,
)
from issue_orchestrator.execution.executor_guardian_cancellation import (
    ExecutorSessionGuardianCanceller,
    InteractiveExecutorGuardianCancellationLease,
)
from issue_orchestrator.execution.process_cancellation_endpoint import (
    ProcessCancellationEndpointError,
)
from issue_orchestrator.execution.atomic_record_store import (
    OsAtomicRecordStoreFactory,
)


def _cancellation(run_dir: Path) -> ExecutorInteractiveSessionCancellation:
    return ExecutorInteractiveSessionCancellation.for_run_dir(run_dir.resolve())


def _canceller(seconds: float) -> ExecutorSessionGuardianCanceller:
    return ExecutorSessionGuardianCanceller(seconds, OsAtomicRecordStoreFactory())


@pytest.mark.parametrize("seconds", (0.0, math.nan, math.inf))
def test_canceller_requires_a_finite_positive_force_bound(seconds: float) -> None:
    with pytest.raises(ValueError, match="must be finite and positive"):
        _canceller(seconds)


def test_canceller_reports_an_absent_endpoint_explicitly(tmp_path: Path) -> None:
    outcome = _canceller(1.0).contain_if_active(_cancellation(tmp_path))

    assert outcome is ExecutorSessionContainmentOutcome.ABSENT


def test_canceller_validates_and_retires_a_stale_record(tmp_path: Path) -> None:
    cancellation = _cancellation(tmp_path)
    owner = InteractiveExecutorGuardianCancellationLease(
        cancellation,
        OsAtomicRecordStoreFactory(),
    )
    owner.activate()
    owner.transfer_to_owner()

    outcome = _canceller(1.0).contain_if_active(cancellation)

    assert outcome is ExecutorSessionContainmentOutcome.STALE_RETIRED
    assert not cancellation.record_path.exists()
    owner.retire()


def test_canceller_fails_fast_on_a_malformed_stale_record(tmp_path: Path) -> None:
    cancellation = _cancellation(tmp_path)
    owner = InteractiveExecutorGuardianCancellationLease(
        cancellation,
        OsAtomicRecordStoreFactory(),
    )
    owner.activate()
    valid_record = cancellation.record_path.read_text(encoding="utf-8")
    try:
        cancellation.record_path.write_text(
            '{"guardian_endpoint":"wrong"}\n', encoding="utf-8"
        )
        with pytest.raises(
            ProcessCancellationEndpointError,
            match="invalid process cancellation record",
        ):
            _canceller(1.0).contain_if_active(cancellation)
    finally:
        cancellation.record_path.write_text(valid_record, encoding="utf-8")
        owner.retire()


def test_only_one_guardian_can_own_a_run_cancellation_endpoint(
    tmp_path: Path,
) -> None:
    cancellation = _cancellation(tmp_path)
    owner = InteractiveExecutorGuardianCancellationLease(
        cancellation,
        OsAtomicRecordStoreFactory(),
    )
    try:
        with pytest.raises(
            ProcessCancellationEndpointError,
            match="already owns this record",
        ):
            InteractiveExecutorGuardianCancellationLease(
                cancellation,
                OsAtomicRecordStoreFactory(),
            )
    finally:
        owner.retire()
