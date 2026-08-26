"""Port for the three pipes spanning executor guardian activation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast, Protocol, runtime_checkable

from ..domain.posix_process import PosixDescriptorMapping
from .posix_pipe import PosixPipeReader, PosixPipeWriter


@dataclass(frozen=True, slots=True)
class GuardianChildPipeDescriptors:
    """Exact child endpoints encoded in the guardian wire invocation."""

    result_writer: int
    start_reader: int
    owner_ready_writer: int
    parent_lifetime_reader: int

    def __post_init__(self) -> None:
        values = (
            self.result_writer,
            self.start_reader,
            self.owner_ready_writer,
            self.parent_lifetime_reader,
        )
        if any(type(value) is not int or value < 0 for value in values):
            raise ValueError("guardian child pipe descriptors must be non-negative")
        if len(values) != len(set(values)):
            raise ValueError("guardian child pipe descriptors must be unique")


@dataclass(frozen=True, slots=True)
class GuardianParentPipeEndpoints:
    """Parent endpoints retained after a completely successful transfer."""

    result_reader: PosixPipeReader
    start_writer: PosixPipeWriter
    owner_ready_reader: PosixPipeReader
    parent_lifetime_writer: PosixPipeWriter

    def __post_init__(self) -> None:
        if not isinstance(cast(object, self.result_reader), PosixPipeReader):
            raise ValueError("guardian result reader must implement PosixPipeReader")
        if not isinstance(cast(object, self.start_writer), PosixPipeWriter):
            raise ValueError("guardian start writer must implement PosixPipeWriter")
        if not isinstance(cast(object, self.owner_ready_reader), PosixPipeReader):
            raise ValueError(
                "guardian owner-ready reader must implement PosixPipeReader"
            )
        if not isinstance(cast(object, self.parent_lifetime_writer), PosixPipeWriter):
            raise ValueError(
                "guardian parent-lifetime writer must implement PosixPipeWriter"
            )


@dataclass(frozen=True, slots=True)
class GuardianLaunchPipesClosed:
    """Every child or parent endpoint owned by the bundle was closed."""


@dataclass(frozen=True, slots=True)
class GuardianLaunchPipesCloseFailed:
    """Independent pipe cleanup completed with exact failure evidence."""

    error: BaseException

    def __post_init__(self) -> None:
        if not isinstance(cast(object, self.error), BaseException):
            raise ValueError(
                "GuardianLaunchPipesCloseFailed.error must be an exception"
            )


GuardianLaunchPipesClose = GuardianLaunchPipesClosed | GuardianLaunchPipesCloseFailed


@runtime_checkable
class GuardianLaunchPipes(Protocol):
    """Own guardian launch descriptors and retained parent endpoints."""

    @property
    def child_descriptors(self) -> GuardianChildPipeDescriptors: ...

    def descriptor_mappings(
        self,
        inherited_descriptors: tuple[int, ...],
    ) -> tuple[PosixDescriptorMapping, ...]: ...

    def transfer_parent_endpoints_after_launch(
        self,
    ) -> GuardianParentPipeEndpoints: ...

    def close(self) -> GuardianLaunchPipesClose: ...


@runtime_checkable
class GuardianLaunchPipesFactory(Protocol):
    """Acquire all guardian pipes or close every partial acquisition."""

    def create(self) -> GuardianLaunchPipes: ...
