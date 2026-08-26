"""Unbuffered durable journals with explicitly bounded diagnostic tails."""

from __future__ import annotations

import locale
import os
from dataclasses import dataclass, field
from typing import NoReturn

from ..domain.validation_execution import (
    ValidationCommandOutput,
    ValidationCommandOutputCapture,
)
from ..ports.validation_output_journal import (
    ValidationOutputJournal,
    ValidationOutputJournalFactory,
    ValidationOutputJournalResult,
    ValidationOutputStream,
)


def _combine_failures(failures: tuple[BaseException, ...]) -> BaseException | None:
    if not failures:
        return None
    if len(failures) == 1:
        return failures[0]
    return BaseExceptionGroup(
        "validation output journal failed more than once", failures
    )


def _raise_with_descriptor_cleanup(
    primary: BaseException,
    descriptors: tuple[int, ...],
) -> NoReturn:
    failures: list[BaseException] = [primary]
    for descriptor in descriptors:
        try:
            os.close(descriptor)
        except BaseException as error:
            failures.append(error)
    combined = _combine_failures(tuple(failures))
    if combined is None:
        raise AssertionError("descriptor cleanup requires a primary failure")
    raise combined


@dataclass(slots=True)
class PosixValidationOutputJournal(ValidationOutputJournal):
    """Write every chunk immediately while retaining only a bounded tail."""

    capture: ValidationCommandOutputCapture
    stdout_descriptor: int
    stderr_descriptor: int
    _descriptors: dict[ValidationOutputStream, int] = field(init=False)
    _tails: dict[ValidationOutputStream, bytearray] = field(init=False)
    _totals: dict[ValidationOutputStream, int] = field(init=False)

    def __post_init__(self) -> None:
        if type(self.capture) is not ValidationCommandOutputCapture:
            raise ValueError("validation output journal capture must be typed")
        for field_name, descriptor in (
            ("stdout_descriptor", self.stdout_descriptor),
            ("stderr_descriptor", self.stderr_descriptor),
        ):
            if type(descriptor) is not int or descriptor < 0:
                raise ValueError(f"{field_name} must be an open descriptor")
        self._descriptors = {
            ValidationOutputStream.STDOUT: self.stdout_descriptor,
            ValidationOutputStream.STDERR: self.stderr_descriptor,
        }
        self._tails = {
            ValidationOutputStream.STDOUT: bytearray(),
            ValidationOutputStream.STDERR: bytearray(),
        }
        self._totals = {
            ValidationOutputStream.STDOUT: 0,
            ValidationOutputStream.STDERR: 0,
        }

    def append(self, stream: ValidationOutputStream, payload: bytes) -> None:
        if type(stream) is not ValidationOutputStream:
            raise ValueError("validation output stream must be typed")
        if type(payload) is not bytes or not payload:
            raise ValueError("validation journal payload must be non-empty bytes")
        descriptor = self._descriptors[stream]
        offset = 0
        while offset < len(payload):
            written = os.write(descriptor, payload[offset:])
            if written <= 0:
                raise OSError("validation journal write made no progress")
            durable = payload[offset : offset + written]
            self._retain_tail(stream, durable)
            offset += written

    def finalize(self) -> ValidationOutputJournalResult:
        failures: list[BaseException] = []
        for stream in (ValidationOutputStream.STDOUT, ValidationOutputStream.STDERR):
            descriptor = self._descriptors.pop(stream, None)
            if descriptor is None:
                continue
            try:
                os.fsync(descriptor)
            except BaseException as error:
                failures.append(error)
            try:
                os.close(descriptor)
            except BaseException as error:
                failures.append(error)
        return ValidationOutputJournalResult(
            ValidationCommandOutput(
                self._render_tail(ValidationOutputStream.STDOUT),
                self._render_tail(ValidationOutputStream.STDERR),
            ),
            _combine_failures(tuple(failures)),
        )

    def _retain_tail(self, stream: ValidationOutputStream, payload: bytes) -> None:
        tail = self._tails[stream]
        limit = self.capture.retained_tail_bytes
        self._totals[stream] += len(payload)
        if len(payload) >= limit:
            tail[:] = payload[-limit:]
            return
        overflow = len(tail) + len(payload) - limit
        if overflow > 0:
            del tail[:overflow]
        tail.extend(payload)

    def _render_tail(self, stream: ValidationOutputStream) -> str:
        tail = bytes(self._tails[stream])
        total = self._totals[stream]
        encoding = locale.getpreferredencoding(False)
        text = tail.decode(encoding, errors="replace")
        if total == len(tail):
            return text
        return (
            "[VALIDATION OUTPUT TRUNCATED: retained last "
            f"{len(tail)} of {total} bytes]\n{text}"
        )


class PosixValidationOutputJournalFactory(ValidationOutputJournalFactory):
    """Allocate both journal descriptors before returning ownership."""

    def create(
        self,
        capture: ValidationCommandOutputCapture,
    ) -> ValidationOutputJournal:
        if type(capture) is not ValidationCommandOutputCapture:
            raise ValueError("validation output capture must be typed")
        capture.stdout_path.parent.mkdir(parents=True, exist_ok=True)
        capture.stderr_path.parent.mkdir(parents=True, exist_ok=True)
        stdout_descriptor = os.open(
            capture.stdout_path,
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_CLOEXEC,
            0o600,
        )
        try:
            stderr_descriptor = os.open(
                capture.stderr_path,
                os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_CLOEXEC,
                0o600,
            )
        except BaseException as stderr_error:
            _raise_with_descriptor_cleanup(stderr_error, (stdout_descriptor,))
        try:
            return PosixValidationOutputJournal(
                capture,
                stdout_descriptor,
                stderr_descriptor,
            )
        except BaseException as setup_error:
            _raise_with_descriptor_cleanup(
                setup_error,
                (stdout_descriptor, stderr_descriptor),
            )
