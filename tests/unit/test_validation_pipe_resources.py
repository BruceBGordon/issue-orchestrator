"""Behavior tests for total validation-pipe resource ownership."""

from __future__ import annotations

import selectors
from dataclasses import dataclass, field
from enum import StrEnum
from typing import BinaryIO, cast

import pytest

from issue_orchestrator.execution.validation_pipe_resources import (
    ValidationPipeResourceOwner,
    ValidationPipeRole,
)


class _FailurePoint(StrEnum):
    REGISTER_STDOUT = "register-stdout"
    REGISTER_STDERR = "register-stderr"
    REGISTER_HANDSHAKE = "register-handshake"
    UNREGISTER_STDOUT = "unregister-stdout"
    UNREGISTER_STDERR = "unregister-stderr"
    UNREGISTER_HANDSHAKE = "unregister-handshake"
    CLOSE_STDOUT = "close-stdout"
    CLOSE_STDERR = "close-stderr"
    CLOSE_HANDSHAKE = "close-handshake"
    CLOSE_SELECTOR = "close-selector"


_DESCRIPTOR_ROLE = {
    101: ValidationPipeRole.STDOUT,
    102: ValidationPipeRole.STDERR,
    103: ValidationPipeRole.EXECUTOR_HANDSHAKE,
}


def _failure_point(action: str, role: ValidationPipeRole) -> _FailurePoint:
    return _FailurePoint(f"{action}-{role.value.replace('executor-', '')}")


@dataclass(slots=True)
class _RecordingStream:
    role: ValidationPipeRole
    descriptor: int
    failures: frozenset[_FailurePoint]
    actions: list[str]

    def fileno(self) -> int:
        return self.descriptor

    def close(self) -> None:
        self.actions.append(f"close-{self.role.value}")
        if _failure_point("close", self.role) in self.failures:
            raise RuntimeError(f"injected close failure for {self.role.value}")


@dataclass(slots=True)
class _RecordingSelector:
    failures: frozenset[_FailurePoint]
    actions: list[str]
    registered: set[int] = field(default_factory=set)

    def register(
        self,
        fileobj: int,
        events: int,
        data: object | None = None,
    ) -> selectors.SelectorKey:
        del events, data
        role = _DESCRIPTOR_ROLE[fileobj]
        self.actions.append(f"register-{role.value}")
        if _failure_point("register", role) in self.failures:
            raise RuntimeError(f"injected register failure for {role.value}")
        self.registered.add(fileobj)
        return selectors.SelectorKey(fileobj, fileobj, selectors.EVENT_READ, None)

    def unregister(self, fileobj: int) -> selectors.SelectorKey:
        role = _DESCRIPTOR_ROLE[fileobj]
        self.actions.append(f"unregister-{role.value}")
        if _failure_point("unregister", role) in self.failures:
            raise RuntimeError(f"injected unregister failure for {role.value}")
        self.registered.remove(fileobj)
        return selectors.SelectorKey(fileobj, fileobj, selectors.EVENT_READ, None)

    def select(
        self,
        timeout: float | None = None,
    ) -> list[tuple[selectors.SelectorKey, int]]:
        del timeout
        return []

    def close(self) -> None:
        self.actions.append("close-selector")
        if _FailurePoint.CLOSE_SELECTOR in self.failures:
            raise RuntimeError("injected selector close failure")
        self.registered.clear()


def _resources(
    failures: frozenset[_FailurePoint],
    actions: list[str],
) -> ValidationPipeResourceOwner:
    streams = tuple(
        _RecordingStream(role, descriptor, failures, actions)
        for descriptor, role in _DESCRIPTOR_ROLE.items()
    )
    selector = _RecordingSelector(failures, actions)
    return ValidationPipeResourceOwner(
        cast(BinaryIO, streams[0]),
        cast(BinaryIO, streams[1]),
        cast(BinaryIO, streams[2]),
        lambda: selector,
    )


@pytest.mark.parametrize(
    "failure",
    (
        _FailurePoint.UNREGISTER_STDOUT,
        _FailurePoint.UNREGISTER_STDERR,
        _FailurePoint.UNREGISTER_HANDSHAKE,
        _FailurePoint.CLOSE_STDOUT,
        _FailurePoint.CLOSE_STDERR,
        _FailurePoint.CLOSE_HANDSHAKE,
        _FailurePoint.CLOSE_SELECTOR,
    ),
)
def test_close_attempts_every_phase_and_returns_failure(
    failure: _FailurePoint,
) -> None:
    actions: list[str] = []
    owner = _resources(frozenset((failure,)), actions)

    cleanup_failure = owner.close()

    assert type(cleanup_failure) is RuntimeError
    assert actions[-7:] == [
        "unregister-stdout",
        "unregister-stderr",
        "unregister-executor-handshake",
        "close-stdout",
        "close-stderr",
        "close-executor-handshake",
        "close-selector",
    ]
    assert owner.close() is None
    assert actions.count("close-selector") == 1


@pytest.mark.parametrize(
    "failure",
    (
        _FailurePoint.REGISTER_STDOUT,
        _FailurePoint.REGISTER_STDERR,
        _FailurePoint.REGISTER_HANDSHAKE,
    ),
)
def test_registration_failure_closes_every_acquired_resource(
    failure: _FailurePoint,
) -> None:
    actions: list[str] = []

    with pytest.raises(RuntimeError, match="injected register failure"):
        _resources(frozenset((failure,)), actions)

    assert "close-stdout" in actions
    assert "close-stderr" in actions
    assert "close-executor-handshake" in actions
    assert actions[-1] == "close-selector"


def test_multiple_close_failures_are_aggregated_after_every_attempt() -> None:
    actions: list[str] = []
    owner = _resources(
        frozenset(
            (
                _FailurePoint.UNREGISTER_STDOUT,
                _FailurePoint.CLOSE_STDERR,
                _FailurePoint.CLOSE_SELECTOR,
            )
        ),
        actions,
    )

    failure = owner.close()

    assert type(failure) is ExceptionGroup
    assert len(failure.exceptions) == 3
    assert actions[-1] == "close-selector"
