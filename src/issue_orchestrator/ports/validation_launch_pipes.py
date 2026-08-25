"""Port for the three pipes owned during validation process activation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Protocol, runtime_checkable

from ..domain.posix_process import (
    PosixDescriptorMapping,
    PosixProcessEnvironment,
)
from ..ports.posix_pipe import PosixPipeReader


@dataclass(frozen=True, slots=True)
class ValidationLaunchReaders:
    """All three parent readers after complete ownership transfer."""

    stdout: PosixPipeReader
    stderr: PosixPipeReader
    executor_handshake: PosixPipeReader

    def __post_init__(self) -> None:
        for field_name, reader in (
            ("stdout", self.stdout),
            ("stderr", self.stderr),
            ("executor_handshake", self.executor_handshake),
        ):
            if not isinstance(reader, PosixPipeReader):
                raise ValueError(
                    f"ValidationLaunchReaders.{field_name} must implement "
                    "PosixPipeReader"
                )


@dataclass(frozen=True, slots=True)
class ValidationLaunchPipesClosed:
    """Every pipe endpoint or transferred reader was closed."""


@dataclass(frozen=True, slots=True)
class ValidationLaunchPipesCloseFailed:
    """Total cleanup completed with exact failure evidence."""

    error: BaseException

    def __post_init__(self) -> None:
        if not isinstance(self.error, BaseException):
            raise ValueError(
                "ValidationLaunchPipesCloseFailed.error must be an exception"
            )


ValidationLaunchPipesClose = (
    ValidationLaunchPipesClosed | ValidationLaunchPipesCloseFailed
)


@runtime_checkable
class ValidationLaunchPipes(Protocol):
    """Own launch mappings, handshake environment, and parent readers."""

    @property
    def descriptor_mappings(self) -> tuple[PosixDescriptorMapping, ...]: ...

    def child_environment(
        self,
        base_environment: Mapping[str, str],
    ) -> PosixProcessEnvironment: ...

    def transfer_readers_after_launch(self) -> ValidationLaunchReaders: ...

    def close(self) -> ValidationLaunchPipesClose: ...


@runtime_checkable
class ValidationLaunchPipesFactory(Protocol):
    """Acquire all validation launch pipes or close every partial acquisition."""

    def create(self) -> ValidationLaunchPipes: ...
