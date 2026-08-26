"""Ports for interruption-safe POSIX pipe ownership."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..domain.posix_pipe import PosixPipeClose


@runtime_checkable
class PosixPipeReader(Protocol):
    """Minimal binary reader transferred from a retained pipe."""

    def fileno(self) -> int: ...

    def close(self) -> None: ...


@runtime_checkable
class PosixPipeWriter(Protocol):
    """Minimal binary writer transferred from a retained pipe."""

    def fileno(self) -> int: ...

    def close(self) -> None: ...


@runtime_checkable
class PosixPipe(Protocol):
    """One pipe whose descriptor ownership cannot be ambiguous."""

    @property
    def read_descriptor(self) -> int: ...

    @property
    def write_descriptor(self) -> int: ...

    def transfer_reader_after_launch(self) -> PosixPipeReader:
        """Close the child endpoint and transfer the parent reader."""
        ...

    def transfer_writer_after_launch(self) -> PosixPipeWriter:
        """Close the parent reader and transfer the parent writer."""
        ...

    def close(self) -> PosixPipeClose:
        """Attempt all still-owned descriptor cleanup."""
        ...


@runtime_checkable
class PosixPipeFactory(Protocol):
    """Acquire one kernel pipe behind a fault-injectable system seam."""

    def open(self) -> PosixPipe: ...
