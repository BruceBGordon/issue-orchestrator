# pyright: strict
"""Interruption-safe implementation of the typed POSIX pipe port."""

from __future__ import annotations

import os
import signal

from ..domain.posix_pipe import (
    PosixPipeClosed,
    PosixPipeCloseFailed,
)
from ..ports.posix_pipe import PosixPipe, PosixPipeReader, PosixPipeWriter
from .independent_cleanup import (
    CleanupAction,
    CleanupFailed,
    CleanupSucceeded,
    IndependentCleanupPlan,
)


_TRANSFER_SIGNALS = (signal.SIGHUP, signal.SIGINT, signal.SIGTERM)


def _combine_errors(
    message: str,
    first: BaseException,
    *others: BaseException | None,
) -> BaseException:
    errors = (first, *(error for error in others if error is not None))
    if len(errors) == 1:
        return first
    return BaseExceptionGroup(message, errors)


def _restore_mask(
    previous_mask: set[int | signal.Signals],
) -> BaseException | None:
    try:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
    except BaseException as error:
        return error
    return None


def _close_reader(reader: PosixPipeReader | None) -> BaseException | None:
    if reader is None:
        return None
    try:
        reader.close()
    except BaseException as error:
        return error
    return None


def _close_writer(writer: PosixPipeWriter | None) -> BaseException | None:
    if writer is None:
        return None
    try:
        writer.close()
    except BaseException as error:
        return error
    return None


class OwnedPosixPipe:
    """Own both endpoints until a signal-masked reader transfer succeeds."""

    def __init__(self, read_descriptor: int, write_descriptor: int) -> None:
        if type(read_descriptor) is not int or read_descriptor < 0:
            raise ValueError("OwnedPosixPipe.read_descriptor must be non-negative")
        if type(write_descriptor) is not int or write_descriptor < 0:
            raise ValueError("OwnedPosixPipe.write_descriptor must be non-negative")
        if read_descriptor == write_descriptor:
            raise ValueError("OwnedPosixPipe descriptors must be distinct")
        self._read_descriptor = read_descriptor
        self._write_descriptor = write_descriptor

    @property
    def read_descriptor(self) -> int:
        if self._read_descriptor < 0:
            raise RuntimeError("POSIX pipe reader ownership was transferred")
        return self._read_descriptor

    @property
    def write_descriptor(self) -> int:
        if self._write_descriptor < 0:
            raise RuntimeError("POSIX pipe writer is closed")
        return self._write_descriptor

    def transfer_reader_after_launch(self) -> PosixPipeReader:
        previous_mask = signal.pthread_sigmask(
            signal.SIG_BLOCK,
            _TRANSFER_SIGNALS,
        )
        reader: PosixPipeReader | None = None
        try:
            os.close(self._write_descriptor)
            self._write_descriptor = -1
            reader = os.fdopen(self._read_descriptor, "rb", buffering=0)
            self._read_descriptor = -1
        except BaseException as transfer_error:
            raise _combine_errors(
                "POSIX pipe reader transfer failed",
                transfer_error,
                _restore_mask(previous_mask),
                _close_reader(reader),
            )
        restoration_error = _restore_mask(previous_mask)
        if restoration_error is not None:
            raise _combine_errors(
                "POSIX pipe signal restoration failed",
                restoration_error,
                _close_reader(reader),
            )
        return reader

    def transfer_writer_after_launch(self) -> PosixPipeWriter:
        previous_mask = signal.pthread_sigmask(
            signal.SIG_BLOCK,
            _TRANSFER_SIGNALS,
        )
        writer: PosixPipeWriter | None = None
        try:
            os.close(self._read_descriptor)
            self._read_descriptor = -1
            writer = os.fdopen(self._write_descriptor, "wb", buffering=0)
            self._write_descriptor = -1
        except BaseException as transfer_error:
            raise _combine_errors(
                "POSIX pipe writer transfer failed",
                transfer_error,
                _restore_mask(previous_mask),
                _close_writer(writer),
            )
        restoration_error = _restore_mask(previous_mask)
        if restoration_error is not None:
            raise _combine_errors(
                "POSIX pipe signal restoration failed",
                restoration_error,
                _close_writer(writer),
            )
        return writer

    def close(self) -> PosixPipeClosed | PosixPipeCloseFailed:
        actions = tuple(
            CleanupAction(
                f"posix-pipe-fd-{descriptor}-close",
                lambda fd=descriptor: os.close(fd),
            )
            for descriptor in (self._read_descriptor, self._write_descriptor)
            if descriptor >= 0
        )
        self._read_descriptor = -1
        self._write_descriptor = -1
        outcome = IndependentCleanupPlan(actions).run()
        if type(outcome) is CleanupSucceeded:
            return PosixPipeClosed()
        if type(outcome) is not CleanupFailed:
            raise AssertionError("cleanup outcome is a closed union")
        errors = tuple(failure.error for failure in outcome.failures)
        error = (
            errors[0]
            if len(errors) == 1
            else BaseExceptionGroup("POSIX pipe cleanup failed", errors)
        )
        return PosixPipeCloseFailed(error)


class OsPosixPipeFactory:
    """Acquire kernel pipes for production process activation."""

    def open(self) -> PosixPipe:
        read_descriptor, write_descriptor = os.pipe()
        return OwnedPosixPipe(read_descriptor, write_descriptor)
