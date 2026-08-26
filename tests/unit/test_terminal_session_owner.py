"""Public fault proofs for terminal-session launch preparation."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import pytest

from issue_orchestrator.domain.process_group_sentinel import (
    ProcessGroupSentinelProgram,
)
from issue_orchestrator.domain.terminal_session_owner import (
    TerminalSessionOwnerPolicy,
)
from issue_orchestrator.domain.terminal_session_termination import (
    TerminalSessionOwnerCancellation,
)
from issue_orchestrator.execution.atomic_record_store import (
    OsAtomicRecordStoreFactory,
)
from issue_orchestrator.execution.process_cancellation_endpoint import (
    PosixProcessCancellationEndpointLeaseFactory,
    ProcessCancellationEndpointLeaseContract,
    ProcessCancellationEndpointLeaseFactory,
    ProcessCancellationOwnerControls,
)
from issue_orchestrator.execution.terminal_session_owner import (
    PosixTerminalSessionOwner,
    TerminalSessionOwnerProgram,
)
from issue_orchestrator.ports.atomic_record_store import (
    AtomicRecordStoreFactory,
)


@dataclass(slots=True)
class _LeaseRetirementProbe:
    retire_calls: int = 0


class _DelegatingLease:
    def __init__(
        self,
        delegate: ProcessCancellationEndpointLeaseContract,
        retirement: _LeaseRetirementProbe,
    ) -> None:
        self._delegate = delegate
        self._retirement = retirement

    def controls(self) -> ProcessCancellationOwnerControls:
        return self._delegate.controls()

    def transfer_after_inherited_activation(self) -> None:
        self._delegate.transfer_after_inherited_activation()

    def release_parent_after_spawn_uncertainty(self) -> None:
        self._delegate.release_parent_after_spawn_uncertainty()

    def retire(self) -> None:
        self._retirement.retire_calls += 1
        self._delegate.retire()


class _ControlsFailureLease(_DelegatingLease):
    def __init__(
        self,
        delegate: ProcessCancellationEndpointLeaseContract,
        retirement: _LeaseRetirementProbe,
        controls_error: BaseException,
    ) -> None:
        super().__init__(delegate, retirement)
        self._controls_error = controls_error

    def controls(self) -> ProcessCancellationOwnerControls:
        raise self._controls_error


class _ControlsAndRetirementFailureLease(_ControlsFailureLease):
    def __init__(
        self,
        delegate: ProcessCancellationEndpointLeaseContract,
        retirement: _LeaseRetirementProbe,
        controls_error: BaseException,
        retirement_error: BaseException,
    ) -> None:
        super().__init__(delegate, retirement, controls_error)
        self._retirement_error = retirement_error

    def retire(self) -> None:
        super().retire()
        raise self._retirement_error


@dataclass(frozen=True, slots=True)
class _InvalidControls:
    listener_file_descriptor: int = -1
    owner_lock_file_descriptor: int = -1


class _InvalidControlsLease(_DelegatingLease):
    def controls(self) -> ProcessCancellationOwnerControls:
        return cast(ProcessCancellationOwnerControls, _InvalidControls())


class _IncompleteLaunchLease:
    """Fault adapter rejected by the launch lease's runtime contract check."""

    def __init__(
        self,
        delegate: ProcessCancellationEndpointLeaseContract,
        retirement: _LeaseRetirementProbe,
    ) -> None:
        self._delegate = delegate
        self._retirement = retirement

    def controls(self) -> ProcessCancellationOwnerControls:
        return self._delegate.controls()

    def retire(self) -> None:
        self._retirement.retire_calls += 1
        self._delegate.retire()


class _ControlsFailureLeaseFactory:
    def __init__(
        self,
        retirement: _LeaseRetirementProbe,
        controls_error: BaseException,
    ) -> None:
        self._retirement = retirement
        self._controls_error = controls_error

    def create(
        self,
        record_path: Path,
        record_stores: AtomicRecordStoreFactory,
    ) -> ProcessCancellationEndpointLeaseContract:
        delegate = PosixProcessCancellationEndpointLeaseFactory().create(
            record_path,
            record_stores,
        )
        return _ControlsFailureLease(
            delegate,
            self._retirement,
            self._controls_error,
        )


class _InvalidControlsLeaseFactory:
    def __init__(self, retirement: _LeaseRetirementProbe) -> None:
        self._retirement = retirement

    def create(
        self,
        record_path: Path,
        record_stores: AtomicRecordStoreFactory,
    ) -> ProcessCancellationEndpointLeaseContract:
        delegate = PosixProcessCancellationEndpointLeaseFactory().create(
            record_path,
            record_stores,
        )
        return _InvalidControlsLease(delegate, self._retirement)


class _ControlsAndRetirementFailureLeaseFactory:
    def __init__(
        self,
        retirement: _LeaseRetirementProbe,
        controls_error: BaseException,
        retirement_error: BaseException,
    ) -> None:
        self._retirement = retirement
        self._controls_error = controls_error
        self._retirement_error = retirement_error

    def create(
        self,
        record_path: Path,
        record_stores: AtomicRecordStoreFactory,
    ) -> ProcessCancellationEndpointLeaseContract:
        delegate = PosixProcessCancellationEndpointLeaseFactory().create(
            record_path,
            record_stores,
        )
        return _ControlsAndRetirementFailureLease(
            delegate,
            self._retirement,
            self._controls_error,
            self._retirement_error,
        )


class _IncompleteLaunchLeaseFactory:
    def __init__(self, retirement: _LeaseRetirementProbe) -> None:
        self._retirement = retirement

    def create(
        self,
        record_path: Path,
        record_stores: AtomicRecordStoreFactory,
    ) -> ProcessCancellationEndpointLeaseContract:
        delegate = PosixProcessCancellationEndpointLeaseFactory().create(
            record_path,
            record_stores,
        )
        incomplete = _IncompleteLaunchLease(delegate, self._retirement)
        return cast(ProcessCancellationEndpointLeaseContract, incomplete)


def _owner(
    endpoint_leases: ProcessCancellationEndpointLeaseFactory,
) -> PosixTerminalSessionOwner:
    executable = str(Path(sys.executable).resolve())
    return PosixTerminalSessionOwner(
        TerminalSessionOwnerProgram((executable, "-m", "owner")),
        ProcessGroupSentinelProgram((executable, "-m", "sentinel")),
        TerminalSessionOwnerPolicy(1.0, 1.0),
        OsAtomicRecordStoreFactory(),
        endpoint_leases,
    )


def _assert_endpoint_can_be_reacquired(
    cancellation: TerminalSessionOwnerCancellation,
) -> None:
    launch_lease = _owner(
        PosixProcessCancellationEndpointLeaseFactory()
    ).prepare(("/bin/true",), cancellation)
    launch_lease.retire_after_containment()


def test_controls_failure_retires_the_acquired_endpoint_and_preserves_primary(
    tmp_path: Path,
) -> None:
    retirement = _LeaseRetirementProbe()
    primary_error = RuntimeError("injected controls failure")
    cancellation = TerminalSessionOwnerCancellation.for_run_dir(
        tmp_path.resolve()
    )

    with pytest.raises(RuntimeError) as raised:
        _owner(
            _ControlsFailureLeaseFactory(retirement, primary_error)
        ).prepare(("/bin/true",), cancellation)

    assert raised.value is primary_error
    assert retirement.retire_calls == 1
    assert not cancellation.record_path.exists()
    _assert_endpoint_can_be_reacquired(cancellation)


def test_invocation_failure_retires_the_acquired_endpoint(tmp_path: Path) -> None:
    retirement = _LeaseRetirementProbe()
    cancellation = TerminalSessionOwnerCancellation.for_run_dir(
        tmp_path.resolve()
    )

    with pytest.raises(ValueError, match="listener_file_descriptor"):
        _owner(_InvalidControlsLeaseFactory(retirement)).prepare(
            ("/bin/true",),
            cancellation,
        )

    assert retirement.retire_calls == 1
    assert not cancellation.record_path.exists()
    _assert_endpoint_can_be_reacquired(cancellation)


def test_preparation_preserves_primary_and_endpoint_retirement_failures(
    tmp_path: Path,
) -> None:
    retirement = _LeaseRetirementProbe()
    primary_error = RuntimeError("injected controls failure")
    retirement_error = RuntimeError("injected retirement evidence failure")
    cancellation = TerminalSessionOwnerCancellation.for_run_dir(
        tmp_path.resolve()
    )

    with pytest.raises(BaseExceptionGroup) as raised:
        _owner(
            _ControlsAndRetirementFailureLeaseFactory(
                retirement,
                primary_error,
                retirement_error,
            )
        ).prepare(("/bin/true",), cancellation)

    assert raised.value.exceptions == (primary_error, retirement_error)
    assert retirement.retire_calls == 1
    assert not cancellation.record_path.exists()
    _assert_endpoint_can_be_reacquired(cancellation)


def test_launch_lease_construction_failure_retires_the_acquired_endpoint(
    tmp_path: Path,
) -> None:
    retirement = _LeaseRetirementProbe()
    cancellation = TerminalSessionOwnerCancellation.for_run_dir(
        tmp_path.resolve()
    )

    with pytest.raises(ValueError, match="cancellation lease contract"):
        _owner(_IncompleteLaunchLeaseFactory(retirement)).prepare(
            ("/bin/true",),
            cancellation,
        )

    assert retirement.retire_calls == 1
    assert not cancellation.record_path.exists()
    _assert_endpoint_can_be_reacquired(cancellation)
