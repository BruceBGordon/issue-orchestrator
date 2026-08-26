"""Typed fault adapters for public POSIX process-owner tests."""

from __future__ import annotations

from dataclasses import dataclass

from issue_orchestrator.domain.posix_process import PosixProcessLaunchSpec
from issue_orchestrator.ports.posix_process import (
    PosixProcessHandle,
    PosixProcessLaunch,
    PosixProcessLauncher,
    PosixProcessLaunchStarted,
)


@dataclass(frozen=True, slots=True)
class ReapEvidenceFailingProcessHandle(PosixProcessHandle):
    """Close the delegate's ownership, then report injected evidence failure."""

    delegate: PosixProcessHandle
    failure: RuntimeError

    def __post_init__(self) -> None:
        if not isinstance(self.delegate, PosixProcessHandle):
            raise ValueError("reap-evidence delegate must implement its port")
        if type(self.failure) is not RuntimeError:
            raise ValueError("reap-evidence failure must be RuntimeError")

    @property
    def process_id(self) -> int:
        return self.delegate.process_id

    @property
    def return_code(self) -> int | None:
        return self.delegate.return_code

    def poll(self) -> int | None:
        return self.delegate.poll()

    def wait(self, timeout_seconds: float) -> int:
        return self.delegate.wait(timeout_seconds)

    def kill(self) -> None:
        self.delegate.kill()

    def record_external_reap(self, exit_code: int) -> None:
        self.delegate.record_external_reap(exit_code)
        raise self.failure


@dataclass(frozen=True, slots=True)
class ReapEvidenceFailingProcessLauncher(PosixProcessLauncher):
    """Wrap each successfully retained handle in the evidence fault adapter."""

    delegate: PosixProcessLauncher
    failure: RuntimeError

    def __post_init__(self) -> None:
        if not isinstance(self.delegate, PosixProcessLauncher):
            raise ValueError("reap-evidence launcher must implement its port")
        if type(self.failure) is not RuntimeError:
            raise ValueError("reap-evidence launcher failure must be RuntimeError")

    def launch(self, specification: PosixProcessLaunchSpec) -> PosixProcessLaunch:
        launch = self.delegate.launch(specification)
        if type(launch) is not PosixProcessLaunchStarted:
            return launch
        return PosixProcessLaunchStarted(
            ReapEvidenceFailingProcessHandle(launch.process, self.failure)
        )
