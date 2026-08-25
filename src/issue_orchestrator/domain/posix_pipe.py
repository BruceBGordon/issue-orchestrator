"""Strong terminal facts for one owned POSIX pipe."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PosixPipeDescriptors:
    """The exact read and write descriptors returned by the kernel."""

    read_descriptor: int
    write_descriptor: int

    def __post_init__(self) -> None:
        for field_name, value in (
            ("read_descriptor", self.read_descriptor),
            ("write_descriptor", self.write_descriptor),
        ):
            if type(value) is not int or value < 0:
                raise ValueError(
                    f"PosixPipeDescriptors.{field_name} must be non-negative"
                )
        if self.read_descriptor == self.write_descriptor:
            raise ValueError("POSIX pipe descriptors must be distinct")


@dataclass(frozen=True, slots=True)
class PosixPipeClosed:
    """Every descriptor still owned by the pipe was closed."""


@dataclass(frozen=True, slots=True)
class PosixPipeCloseFailed:
    """Independent pipe cleanup completed with exact failure evidence."""

    error: BaseException

    def __post_init__(self) -> None:
        if not isinstance(self.error, BaseException):
            raise ValueError("PosixPipeCloseFailed.error must be an exception")


PosixPipeClose = PosixPipeClosed | PosixPipeCloseFailed
