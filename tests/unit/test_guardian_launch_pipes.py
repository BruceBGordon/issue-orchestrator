"""Public-boundary proofs for all-or-nothing guardian pipe acquisition."""

from __future__ import annotations

import os

import pytest

from issue_orchestrator.execution.guardian_launch_pipes import (
    PosixGuardianLaunchPipesFactory,
)
from issue_orchestrator.execution.posix_pipe import OsPosixPipeFactory
from issue_orchestrator.ports.posix_pipe import PosixPipe, PosixPipeFactory


class _FailingThirdPipeFactory:
    """Acquire two real pipes, then fail through the public factory port."""

    def __init__(self) -> None:
        self._delegate: PosixPipeFactory = OsPosixPipeFactory()
        self._calls = 0

    def open(self) -> PosixPipe:
        self._calls += 1
        if self._calls == 3:
            raise RuntimeError("injected third guardian pipe acquisition failure")
        return self._delegate.open()


def test_partial_guardian_pipe_acquisition_closes_every_endpoint() -> None:
    before = len(os.listdir("/dev/fd"))

    with pytest.raises(
        RuntimeError,
        match="injected third guardian pipe acquisition failure",
    ):
        PosixGuardianLaunchPipesFactory(_FailingThirdPipeFactory()).create()

    assert len(os.listdir("/dev/fd")) == before
